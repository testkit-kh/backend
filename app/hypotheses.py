"""
Роутер бизнес-логики — гипотезы, сертификаты, слои карты.

Все эндпоинты живут под ``/api/v1`` и требуют JWT.
Ролевой контроль — внутри каждого обработчика.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from geoalchemy2 import Geography
from geoalchemy2.functions import (
    ST_AsGeoJSON,
    ST_DWithin,
    ST_Intersects,
    ST_MakePoint,
    ST_SetSRID,
)
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.age import has_field_access
from app.analytics import EventType, emit
from app.auth import get_current_user
from app.cleanup_cost import estimate_cleanup
from app.config import settings
from app.database import get_session
from app.models import (
    CertificateStatus,
    Event,
    EventStatus,
    Hypothesis,
    HypothesisStatus,
    MonitoringSite,
    Notification,
    NotificationKind,
    Organization,
    Staff,
    User,
    UserRole,
    Volunteer,
)
from app.moderation import log_moderation
from app.schemas import (
    GeoJSONFeature,
    GeoJSONFeatureCollection,
    GeoJSONGeometry,
    GeoJSONProperties,
    HypothesisCreateRequest,
    HypothesisOut,
    HypothesisValidateRequest,
    HypothesisValidateResponse,
    MyHypothesesListOut,
    MyHypothesisOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1",
    tags=["business-logic"],
)

# Порог в секундах для определения офлайн-режима.
# Если клиентское время старше серверного более чем
# на эту величину — точка была в очереди.
_OFFLINE_THRESHOLD = timedelta(minutes=5)


# ═══════════════════════════════════════════════════════════
# Хелперы — role guards
# ═══════════════════════════════════════════════════════════


def _require_volunteer(user: User) -> Volunteer:
    """Волонтёрский профиль или 403."""
    if user.role != UserRole.volunteer or not user.volunteer:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступно только для волонтёров.",
        )
    return user.volunteer


def _require_staff(user: User) -> Staff:
    """Профиль сотрудника или 403."""
    if user.role != UserRole.staff or not user.staff:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступно только для сотрудников ООПТ.",
        )
    return user.staff


# ═══════════════════════════════════════════════════════════
# Лента «Мои точки»: курсор и уведомление о вердикте
# ═══════════════════════════════════════════════════════════


def encode_hypothesis_cursor(created_at: datetime, hypothesis_id: uuid.UUID) -> str:
    """Непрозрачный курсор: created_at + id, чтобы страница не плыла."""
    raw = f"{created_at.isoformat()}|{hypothesis_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_hypothesis_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Разбирает курсор или отвечает 400: битый курсор — не 500."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        stamp, raw_id = raw.split("|", 1)
        created_at = datetime.fromisoformat(stamp)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at, uuid.UUID(raw_id)
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cursor.",
        ) from error


_STATUS_NOTICE: dict[HypothesisStatus, tuple[str, str]] = {
    HypothesisStatus.approved: (
        "Точка принята",
        "ООПТ подтвердила находку и готовит выезд на уборку.",
    ),
    HypothesisStatus.rejected: (
        "Точка отклонена",
        "ООПТ не приняла точку. Причину смотрите в ленте «Мои точки».",
    ),
    HypothesisStatus.drone_requested: (
        "По точке запрошена аэросъёмка",
        "Сотрудник ООПТ запросил аэросъёмку этой находки.",
    ),
    HypothesisStatus.cleaned: (
        "Точка убрана",
        "Мусор с вашей точки вывезен — спасибо, что отметили место.",
    ),
}


def notify_point_status_changed(
    session: AsyncSession,
    *,
    author_id: uuid.UUID | None,
    hypothesis_id: uuid.UUID,
    new_status: HypothesisStatus,
    reject_reason: str | None = None,
) -> None:
    """In-app уведомление автору точки.

    Аналитическое `point_validated` само по себе колокольчик не звонит:
    волонтёр увидел бы вердикт, только если сам открыл ленту. Тип
    `point_validated` в enum как раз для этого письма в колокольчик.

    author_id пуст после удаления аккаунта — писать некуда и некому.
    """
    if author_id is None:
        return
    title, default_body = _STATUS_NOTICE.get(
        new_status,
        ("Статус точки изменился", "Откройте ленту «Мои точки», чтобы увидеть вердикт."),
    )
    body = (
        reject_reason if new_status == HypothesisStatus.rejected and reject_reason else default_body
    )
    session.add(
        Notification(
            user_id=author_id,
            kind=NotificationKind.point_validated,
            title=title,
            body=body,
            action_url="/hypotheses/my",
            payload={
                "hypothesis_id": str(hypothesis_id),
                "status": new_status.value,
            },
        )
    )


# ═══════════════════════════════════════════════════════════
# Вспомогательные функции для create_hypothesis
# ═══════════════════════════════════════════════════════════


def _extract_lat_lon(body: HypothesisCreateRequest):
    """Извлечь lat/lon: из geometry (centroid) или из полей.

    Geometry имеет приоритет — поля lat/lon остаются для
    обратной совместимости со старым клиентом.
    """
    if body.geometry is not None:
        if body.geometry.type == "Point":
            lon = body.geometry.coordinates[0]
            lat = body.geometry.coordinates[1]
            return lat, lon
        # Для полигона lat/lon обязательны — они задают
        # «метку» точки на карте, а полигон хранится в geom.
        if body.lat is not None and body.lon is not None:
            return body.lat, body.lon
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=("Для Polygon нужны lat + lon (метка точки на карте)."),
        )
    # Обратная совместимость — валидатор гарантирует,
    # что lat и lon не None, если geometry отсутствует.
    return body.lat, body.lon


def _build_geom_wkt(body: HypothesisCreateRequest):
    """Сериализовать клиентскую geometry в GeoJSON-строку
    для ST_GeomFromGeoJSON. None, если не передана.
    """
    if body.geometry is None:
        return None
    return json.dumps(
        {
            "type": body.geometry.type,
            "coordinates": body.geometry.coordinates,
        }
    )


async def _find_org_with_buffer(
    session: AsyncSession,
    point,
) -> uuid.UUID | None:
    """P0-3: трёхступенчатый поиск ООПТ.

    1) ST_Intersects — точка внутри полигона.
    2) ST_DWithin   — точка в прибрежной буферной зоне.
    3) None         — создаём без привязки к ООПТ.
    """
    # Шаг 1: точное попадание
    q = (
        select(Organization.id)
        .where(
            Organization.territory_geom.isnot(None),
            ST_Intersects(
                Organization.territory_geom,
                point,
            ),
        )
        .limit(1)
    )
    result = await session.execute(q)
    org_id = result.scalar_one_or_none()
    if org_id is not None:
        return org_id

    # Шаг 2: буферная зона. ST_DWithin на geography
    # принимает расстояние в метрах. Кастим geometry
    # в geography через ::geography (SQL cast).
    buffer_m = settings.COASTAL_BUFFER_KM * 1000
    geog_territory = Organization.territory_geom.cast(
        Geography(srid=4326),
    )
    geog_point = func.ST_SetSRID(
        point,
        4326,
    ).cast(Geography(srid=4326))
    q_buf = (
        select(Organization.id)
        .where(
            Organization.territory_geom.isnot(None),
            ST_DWithin(
                geog_territory,
                geog_point,
                buffer_m,
            ),
        )
        .limit(1)
    )
    result = await session.execute(q_buf)
    org_id = result.scalar_one_or_none()
    if org_id is not None:
        logger.info(
            "Точка попала в буферную зону (%.1f км) ООПТ %s",
            settings.COASTAL_BUFFER_KM,
            org_id,
        )
        return org_id

    # Шаг 3: ни одна ООПТ не найдена — допустимо
    logger.info(
        "Точка не попала ни в одну ООПТ; создаём с organization_id = NULL.",
    )
    return None


def _compute_offline_payload(
    body: HypothesisCreateRequest,
    now: datetime,
) -> dict:
    """Определить, была ли точка создана офлайн.

    Если created_at_client старше серверного времени более
    чем на 5 минут — добавляем mode: offline и считаем
    queued_seconds.
    """
    if body.created_at_client is None:
        return {"mode": "online"}
    delta = now - body.created_at_client
    if delta > _OFFLINE_THRESHOLD:
        return {
            "mode": "offline",
            "queued_seconds": delta.total_seconds(),
        }
    return {"mode": "online"}


# ═══════════════════════════════════════════════════════════
# 1. POST /api/v1/hypotheses
# ═══════════════════════════════════════════════════════════


@router.post(
    "/hypotheses",
    response_model=HypothesisOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать гипотезу (экологическое наблюдение)",
)
async def create_hypothesis(
    body: HypothesisCreateRequest,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vol = _require_volunteer(user)

    # ---- Проверка доступа: согласие и обучение ----
    if not has_field_access(vol):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Нужно согласие законного представителя:"
                " участникам до 18 лет работа с картой"
                " открывается после его подтверждения."
            ),
        )
    if vol.certificate_status != CertificateStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Карта открывается после проверки"
                " сертификата о прохождении курса."
                f" Текущий статус:"
                f" {vol.certificate_status.value}."
            ),
        )

    # ---- P0-1: идемпотентность офлайна ----
    # Если client_id уже есть у этого автора — вернуть
    # существующую запись с 200 (не 201, не 409).
    if body.client_id is not None:
        existing_q = select(Hypothesis).where(
            Hypothesis.author_id == user.id,
            Hypothesis.client_id == body.client_id,
        )
        existing = await session.execute(existing_q)
        dup = existing.scalar_one_or_none()
        if dup is not None:
            # 200, а не 201: клиенту ясно, что это не новая
            response.status_code = status.HTTP_200_OK
            return HypothesisOut.model_validate(dup)

    # ---- Координаты ----
    lat, lon = _extract_lat_lon(body)
    point = ST_SetSRID(
        ST_MakePoint(lon, lat),
        4326,
    )

    # ---- P0-3: поиск ООПТ с буферной зоной ----
    org_id = await _find_org_with_buffer(
        session,
        point,
    )

    # ---- Мониторинговая площадка ----
    if body.monitoring_site_id is not None:
        site = await session.get(
            MonitoringSite,
            body.monitoring_site_id,
        )
        if site is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Мониторинговая площадка не найдена.",
            )
        # Площадка должна быть из той же ООПТ, что и точка.
        if org_id and site.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=("Площадка принадлежит другой ООПТ, чем координаты точки."),
            )

    # ---- Смета уборки ----
    trash = body.trash
    estimate = None
    if trash.dominant_category is not None and trash.access_type is not None:
        estimate = estimate_cleanup(
            volume_m3=trash.estimated_volume_m3,
            area_m2=trash.estimated_area_m2,
            dominant=trash.dominant_category,
            access=trash.access_type,
        )

    # ---- Построение geom из клиентского GeoJSON ----
    geom_value = None
    geojson_str = _build_geom_wkt(body)
    if geojson_str is not None:
        geom_value = func.ST_SetSRID(
            func.ST_GeomFromGeoJSON(geojson_str),
            4326,
        )

    # ---- Создание записи ----
    now = datetime.now(UTC)
    hypothesis = Hypothesis(
        author_id=user.id,
        organization_id=org_id,
        client_id=body.client_id,
        created_at_client=body.created_at_client,
        lat=lat,
        lon=lon,
        location=func.ST_SetSRID(
            func.ST_MakePoint(lon, lat),
            4326,
        ),
        geom=geom_value,
        description=body.description,
        photo_url=body.photo_url,
        status=HypothesisStatus.pending,
        trash_categories=(
            [c.value for c in trash.trash_categories] if trash.trash_categories else None
        ),
        dominant_category=trash.dominant_category,
        fraction=trash.fraction,
        access_type=trash.access_type,
        estimated_area_m2=trash.estimated_area_m2,
        estimated_volume_m3=trash.estimated_volume_m3,
        computed_volume_m3=(estimate.volume_m3 if estimate else None),
        computed_mass_kg=(estimate.mass_kg if estimate else None),
        cleanup_cost_rub=(estimate.total_rub if estimate else None),
        cost_assumptions=(estimate.assumptions if estimate else None),
        monitoring_site_id=body.monitoring_site_id,
    )
    session.add(hypothesis)
    await session.flush()

    # ---- Аналитика ----
    offline_info = _compute_offline_payload(body, now)
    await emit(
        session,
        EventType.point_created,
        user_id=user.id,
        lat=lat,
        lon=lon,
        payload={
            "hypothesis_id": str(hypothesis.id),
            "organization_id": (str(org_id) if org_id else None),
            **offline_info,
            "has_photo": body.photo_url is not None,
            "trash_categories": (
                [c.value for c in trash.trash_categories] if trash.trash_categories else None
            ),
            "dominant_category": (
                trash.dominant_category.value if trash.dominant_category else None
            ),
            "fraction": (trash.fraction.value if trash.fraction else None),
            "access_type": (trash.access_type.value if trash.access_type else None),
            "volume_m3": (estimate.volume_m3 if estimate else None),
            "mass_kg": (estimate.mass_kg if estimate else None),
            "cleanup_cost_rub": (estimate.total_rub if estimate else None),
            "monitoring_site_id": (
                str(body.monitoring_site_id) if body.monitoring_site_id else None
            ),
        },
    )
    # Зеркальное событие для ООПТ-воронки
    if org_id is not None:
        await emit(
            session,
            EventType.point_received_in_zone,
            user_id=user.id,
            lat=lat,
            lon=lon,
            payload={
                "hypothesis_id": str(hypothesis.id),
                "organization_id": str(org_id),
            },
        )

    return HypothesisOut.model_validate(hypothesis)


# ═══════════════════════════════════════════════════════════════════════════
# 2. GET /api/v1/hypotheses/pending — list pending hypotheses (staff only)
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/hypotheses/pending",
    response_model=list[HypothesisOut],
    summary="List pending hypotheses for the staff member's organization",
)
async def list_pending_hypotheses(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    staff = _require_staff(user)

    query = (
        select(Hypothesis)
        .where(
            Hypothesis.organization_id == staff.organization_id,
            Hypothesis.status == HypothesisStatus.pending,
        )
        .order_by(Hypothesis.created_at.desc())
    )
    result = await session.execute(query)
    rows = result.scalars().all()

    return [HypothesisOut.model_validate(h) for h in rows]


# ═══════════════════════════════════════════════════════════════════════════
# 3. POST /api/v1/hypotheses/{id}/validate — validate hypothesis (staff)
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/hypotheses/{hypothesis_id}/validate",
    response_model=HypothesisValidateResponse,
    summary="Approve, reject, or request a drone survey for a hypothesis",
)
async def validate_hypothesis(
    hypothesis_id: uuid.UUID,
    body: HypothesisValidateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    staff = _require_staff(user)

    # Fetch hypothesis
    result = await session.execute(select(Hypothesis).where(Hypothesis.id == hypothesis_id))
    hypothesis = result.scalar_one_or_none()

    if hypothesis is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hypothesis not found.",
        )

    # Ownership check: staff can only validate hypotheses in their org
    if hypothesis.organization_id != staff.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only validate hypotheses within your organization.",
        )

    # Time from submission to verdict — the operational KPI. Computed here
    # because `updated_at` is about to be overwritten by this very update.
    time_to_validate = (datetime.now(UTC) - hypothesis.created_at).total_seconds()

    previous_status = hypothesis.status
    # Update status
    hypothesis.status = body.status
    # Причина хранится только у отказа: у одобрения её нет, а оставлять
    # текст от предыдущего отказа при пересмотре нельзя — в ленте «Мои
    # точки» он выглядел бы как причина одобрения.
    hypothesis.reject_reason = (
        body.reason.strip() if body.status == HypothesisStatus.rejected and body.reason else None
    )
    await session.flush()

    log_moderation(
        session,
        actor_id=user.id,
        entity_id=hypothesis.id,
        action=body.status.value,
        reason=hypothesis.reject_reason,
    )

    await emit(
        session,
        EventType.point_validated,
        user_id=user.id,
        lat=hypothesis.lat,
        lon=hypothesis.lon,
        payload={
            "hypothesis_id": str(hypothesis.id),
            "organization_id": str(hypothesis.organization_id),
            "author_id": str(hypothesis.author_id),
            "status": body.status.value,
            "time_to_validate": time_to_validate,
        },
    )

    if previous_status != body.status:
        notify_point_status_changed(
            session,
            author_id=hypothesis.author_id,
            hypothesis_id=hypothesis.id,
            new_status=body.status,
            reject_reason=hypothesis.reject_reason,
        )

    # Business rule: if approved → auto-create an Event
    event_id: uuid.UUID | None = None
    if body.status == HypothesisStatus.approved:
        event = Event(
            hypothesis_id=hypothesis.id,
            organization_id=hypothesis.organization_id,
            title=f"Мероприятие по гипотезе: {hypothesis.description[:120]}",
            status=EventStatus.planned,
        )
        session.add(event)
        await session.flush()
        event_id = event.id

        await emit(
            session,
            EventType.cleanup_event_created,
            user_id=user.id,
            payload={
                "event_id": str(event.id),
                "hypothesis_id": str(hypothesis.id),
                "organization_id": str(hypothesis.organization_id),
            },
        )

    return HypothesisValidateResponse(
        hypothesis=HypothesisOut.model_validate(hypothesis),
        event_id=event_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4. GET /api/v1/hypotheses/my — P1-5, лента «Мои точки» (волонтёр)
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/hypotheses/my",
    response_model=MyHypothesesListOut,
    summary="Мои точки: статус, описание и причина отказа",
)
async def list_my_hypotheses(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    status_filter: HypothesisStatus | None = Query(
        default=None,
        alias="status",
        description="Фильтр по статусу точки",
    ),
    limit: int = Query(default=20, ge=1, le=500),
    cursor: str | None = Query(
        default=None,
        description="Курсор следующей страницы из предыдущего ответа",
    ),
):
    """Личная лента автора точек.

    Смысл ручки — обратная связь: человек отправил точку и должен видеть,
    что с ней стало. Поэтому отдаются все статусы, включая rejected с
    причиной: молчаливый отказ — главная причина, по которой волонтёр не
    присылает вторую точку.

    Курсор по (created_at, id), а не offset: между запросами точке могут
    отказать или закрыть мероприятие, и сдвиг «на 20» пропустил бы строку.
    """
    _require_volunteer(user)

    filters = [Hypothesis.author_id == user.id]
    if status_filter is not None:
        filters.append(Hypothesis.status == status_filter)
    if cursor:
        cursor_at, cursor_id = decode_hypothesis_cursor(cursor)
        # Лента новее → старше: следующая страница строго «левее» курсора.
        filters.append(tuple_(Hypothesis.created_at, Hypothesis.id) < (cursor_at, cursor_id))

    query = (
        select(Hypothesis)
        .where(*filters)
        .order_by(Hypothesis.created_at.desc(), Hypothesis.id.desc())
        .limit(limit + 1)
    )
    result = await session.execute(query)
    # unique(): Hypothesis.event и author подтягиваются joined-загрузкой,
    # из-за join строки дублируются на уровне курсора.
    rows = list(result.unique().scalars().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_hypothesis_cursor(last.created_at, last.id)
        rows = rows[:limit]

    # total — по всей ленте (с фильтром статуса), без курсора: клиенту
    # нужно «у вас 47 точек», а не «на этой странице ещё сколько-то».
    total_filters = [Hypothesis.author_id == user.id]
    if status_filter is not None:
        total_filters.append(Hypothesis.status == status_filter)
    total = await session.scalar(select(func.count()).select_from(Hypothesis).where(*total_filters))

    items: list[MyHypothesisOut] = []
    for hypothesis in rows:
        item = MyHypothesisOut.model_validate(hypothesis)
        # Мероприятие уже в памяти (lazy="joined") — отдельных запросов нет.
        event = hypothesis.event
        if event is not None:
            item.event_id = event.id
            item.event_status = event.status
            item.event_scheduled_at = event.scheduled_at
        items.append(item)

    return MyHypothesesListOut(
        items=items,
        total=total or 0,
        limit=limit,
        next_cursor=next_cursor,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5. GET /api/v1/map/layers — GeoJSON for MapLibre
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/map/layers",
    response_model=GeoJSONFeatureCollection,
    summary="GeoJSON layer with ООПТ polygons and approved hypothesis points",
)
async def get_map_layers(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    features: list[GeoJSONFeature] = []

    # --- Layer 1: organization territory polygons --------------------------
    org_query = select(
        Organization.id,
        Organization.name,
        ST_AsGeoJSON(Organization.territory_geom).label("geojson"),
    ).where(Organization.territory_geom.isnot(None))

    org_result = await session.execute(org_query)
    for row in org_result.all():
        geom_dict = json.loads(row.geojson)
        features.append(
            GeoJSONFeature(
                geometry=GeoJSONGeometry(
                    type=geom_dict["type"],
                    coordinates=geom_dict["coordinates"],
                ),
                properties=GeoJSONProperties(
                    id=row.id,
                    name=row.name,
                    layer="oopt_territory",
                ),
            )
        )

    # --- Layer 2: approved hypothesis points/polygons ---------------------
    # Добавляем ST_AsGeoJSON(geom) — если волонтёр отправил полигон разлива,
    # он сохранён в поле geom. Если нет — fallback на Point из lat/lon.
    hyp_query = select(
        Hypothesis.id,
        Hypothesis.lat,
        Hypothesis.lon,
        Hypothesis.description,
        Hypothesis.status,
        ST_AsGeoJSON(Hypothesis.geom).label("geojson"),
    ).where(Hypothesis.status == HypothesisStatus.approved)

    hyp_result = await session.execute(hyp_query)
    for row in hyp_result.all():
        # Проверяем, есть ли полигон (geom не пустой)
        if row.geojson:
            # Полигон есть — используем его оригинальную геометрию
            geom_dict = json.loads(row.geojson)
            geometry = GeoJSONGeometry(
                type=geom_dict["type"],
                coordinates=geom_dict["coordinates"],
            )
        else:
            # Fallback: создаём Point из lat/lon
            geometry = GeoJSONGeometry(
                type="Point",
                coordinates=[row.lon, row.lat],
            )

        features.append(
            GeoJSONFeature(
                geometry=geometry,
                properties=GeoJSONProperties(
                    id=row.id,
                    description=row.description,
                    status=row.status.value,
                    layer="approved_hypothesis",
                ),
            )
        )

    return GeoJSONFeatureCollection(features=features)
