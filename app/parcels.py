"""
Кадастровые участки ООПТ и слой «кому что принадлежит».

Заменяет единственное поле `Organization.cadastral_number`. Территория
организации собирается как объединение геометрий её участков — отсюда же
берётся прибрежная буферная зона, в пределах которой точки волонтёров
считаются относящимися к этой ООПТ.

Слово «морская» из KPI-документа здесь не используется: в географии проекта
есть Байкал, Ладога и Каспий, поэтому буфер считается от берега любого
водоёма и называется прибрежным.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from geoalchemy2 import Geography
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import EventType, emit
from app.auth import get_current_user
from app.database import async_session_factory, get_session
from app.models import CadastralParcel, Organization, ParcelStatus, User, UserRole
from app.rosreestr import (
    ParcelNotFound,
    ParcelResolveFailed,
    is_valid_cadastral_number,
    resolve_parcel,
)
from app.schemas import (
    CadastralParcelCreateRequest,
    CadastralParcelOut,
    ParcelGeometryRequest,
    GeoJSONFeature,
    GeoJSONFeatureCollection,
    GeoJSONGeometry,
    GeoJSONProperties,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["parcels"])


def _require_staff_org(user: User) -> uuid.UUID:
    if user.role != UserRole.staff or user.staff is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only ООПТ staff can manage cadastral parcels.",
        )
    return user.staff.organization_id


# ═══════════════════════════════════════════════════════════════════════════
# Фоновый резолвинг
# ═══════════════════════════════════════════════════════════════════════════

async def resolve_parcel_task(parcel_id: uuid.UUID) -> None:
    """Подтянуть границы участка и пересобрать территорию организации.

    Своя сессия, а не запросовая: задача переживает ответ пользователю.
    Ошибки не пробрасываются — фоновая задача не может ничего сломать в уже
    отданном ответе, но обязана оставить в БД причину неудачи.
    """
    async with async_session_factory() as session:
        parcel = await session.get(CadastralParcel, parcel_id)
        if parcel is None:
            return

        try:
            geometry = await resolve_parcel(parcel.cadastral_number)
        except ParcelNotFound:
            parcel.status = ParcelStatus.failed
            parcel.resolve_error = "Участок с таким номером не найден в ЕГРН"
            await session.commit()
            return
        except ParcelResolveFailed as error:
            parcel.status = ParcelStatus.failed
            parcel.resolve_error = str(error)[:2000]
            await session.commit()
            return

        parcel.geom = func.ST_Multi(
            func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(geometry.geojson)), 4326)
        )
        parcel.status = ParcelStatus.resolved
        parcel.source = geometry.source
        parcel.resolve_error = None
        parcel.resolved_at = datetime.now(UTC)
        await session.flush()

        # Площадь считаем в географии: в градусах она бессмысленна, а
        # гектары нужны и для сметы уборки, и для отчётности ООПТ.
        parcel.area_ha = await session.scalar(
            select(
                func.ST_Area(cast(CadastralParcel.geom, Geography)) / 10_000.0
            ).where(CadastralParcel.id == parcel.id)
        )

        await _rebuild_territory(session, parcel.organization_id)
        await session.commit()


async def _rebuild_territory(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """territory_geom = объединение всех распознанных участков организации."""
    union = await session.scalar(
        select(func.ST_Union(CadastralParcel.geom)).where(
            CadastralParcel.organization_id == organization_id,
            CadastralParcel.geom.isnot(None),
        )
    )
    if union is None:
        return

    organization = await session.get(Organization, organization_id)
    if organization is not None:
        # Объединение участков почти всегда многоконтурное, поэтому колонка
        # territory_geom расширена до MULTIPOLYGON миграцией 0005.
        organization.territory_geom = func.ST_Multi(union)


# ═══════════════════════════════════════════════════════════════════════════
# CRUD участков
# ═══════════════════════════════════════════════════════════════════════════

@router.post(
    "/organizations/me/parcels",
    response_model=CadastralParcelOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Добавить кадастровый участок к своей ООПТ",
)
async def add_parcel(
    body: CadastralParcelCreateRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    organization_id = _require_staff_org(user)
    number = body.cadastral_number.strip()

    if not is_valid_cadastral_number(number):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Формат кадастрового номера: 41:01:0000000:1",
        )

    existing = await session.scalar(
        select(CadastralParcel).where(CadastralParcel.cadastral_number == number)
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Этот участок уже закреплён за организацией.",
        )

    parcel = CadastralParcel(
        organization_id=organization_id,
        cadastral_number=number,
        status=ParcelStatus.pending,
    )
    session.add(parcel)
    await session.flush()

    await emit(
        session,
        EventType.geo_zone_created,
        user_id=user.id,
        payload={
            "kind": "cadastral_parcel",
            "parcel_id": str(parcel.id),
            "cadastral_number": number,
            "organization_id": str(organization_id),
        },
    )

    # 202, а не 201: границы ещё не получены. Фронт показывает участок со
    # статусом «уточняем границы» и обновляет список.
    background.add_task(resolve_parcel_task, parcel.id)

    return CadastralParcelOut.model_validate(parcel)


@router.get(
    "/organizations/me/parcels",
    response_model=list[CadastralParcelOut],
    summary="Участки своей ООПТ",
)
async def list_parcels(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    organization_id = _require_staff_org(user)
    result = await session.execute(
        select(CadastralParcel)
        .where(CadastralParcel.organization_id == organization_id)
        .order_by(CadastralParcel.created_at)
    )
    return [
        CadastralParcelOut.model_validate(p) for p in result.unique().scalars().all()
    ]


@router.post(
    "/parcels/{parcel_id}/retry",
    response_model=CadastralParcelOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Повторить попытку получить границы участка",
)
async def retry_parcel(
    parcel_id: uuid.UUID,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    organization_id = _require_staff_org(user)
    parcel = await session.get(CadastralParcel, parcel_id)
    if parcel is None or parcel.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Parcel not found."
        )

    parcel.status = ParcelStatus.pending
    parcel.resolve_error = None
    await session.flush()
    background.add_task(resolve_parcel_task, parcel.id)

    return CadastralParcelOut.model_validate(parcel)


@router.get(
    "/parcels/resolve-check",
    summary="Диагностика: доступен ли ФГИС ЕГРН и что он отдаёт по номеру",
)
async def resolve_check(
    cadastral_number: str = Query(description="Например, 41:01:0010114:26"),
    user: User = Depends(get_current_user),
):
    """Пробный резолвинг без записи в БД.

    Нужна, потому что ФГИС ЕГРН отвечает по-разному из разных сетей: из-под
    корпоративного VPN или зарубежного хостинга он часто недоступен вовсе.
    Ручка отвечает на вопрос «это у нас код не работает или туда просто не
    пускают», не создавая участок и ничего не меняя.
    """
    _require_staff_org(user)

    started = time.monotonic()
    try:
        geometry = await resolve_parcel(cadastral_number)
    except ParcelNotFound:
        return {
            "cadastral_number": cadastral_number,
            "outcome": "not_found",
            "detail": "ФГИС ответил, но границ по этому номеру нет",
            "elapsed_seconds": round(time.monotonic() - started, 1),
        }
    except ParcelResolveFailed as error:
        return {
            "cadastral_number": cadastral_number,
            "outcome": "unavailable",
            "detail": str(error),
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "hint": "Похоже на сетевую блокировку. Проверьте с другой сети — "
                    "границы всегда можно задать вручную через "
                    "PUT /api/v1/parcels/{id}/geometry",
        }

    coordinates = geometry.geojson.get("coordinates") or []
    return {
        "cadastral_number": cadastral_number,
        "outcome": "ok",
        "geometry_type": geometry.geojson.get("type"),
        "rings": len(coordinates),
        "vertices": sum(len(ring) for poly in coordinates for ring in poly),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "geometry": geometry.geojson,
    }


@router.put(
    "/parcels/{parcel_id}/geometry",
    response_model=CadastralParcelOut,
    summary="Задать границы участка вручную",
)
async def set_parcel_geometry(
    parcel_id: uuid.UUID,
    body: ParcelGeometryRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Границы, введённые человеком.

    Нужны не только когда ФГИС ЕГРН недоступен: у части ООПТ границы вообще не
    описаны кадастровыми участками, а заданы положением о территории. Такие
    контуры взять из Росреестра неоткуда в принципе.
    """
    organization_id = _require_staff_org(user)
    parcel = await session.get(CadastralParcel, parcel_id)
    if parcel is None or parcel.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Parcel not found."
        )

    geometry = func.ST_Multi(
        func.ST_SetSRID(
            func.ST_GeomFromGeoJSON(body.geometry.model_dump_json()), 4326
        )
    )
    parcel.geom = geometry
    parcel.status = ParcelStatus.resolved
    parcel.source = "manual"
    parcel.resolve_error = None
    parcel.resolved_at = datetime.now(UTC)
    await session.flush()

    parcel.area_ha = await session.scalar(
        select(func.ST_Area(cast(CadastralParcel.geom, Geography)) / 10_000.0).where(
            CadastralParcel.id == parcel.id
        )
    )
    await _rebuild_territory(session, organization_id)
    await session.flush()

    return CadastralParcelOut.model_validate(parcel)


@router.delete(
    "/parcels/{parcel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Открепить участок от ООПТ",
)
async def delete_parcel(
    parcel_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    organization_id = _require_staff_org(user)
    parcel = await session.get(CadastralParcel, parcel_id)
    if parcel is None or parcel.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Parcel not found."
        )

    await session.delete(parcel)
    await session.flush()
    # Территория пересобирается сразу: иначе точки продолжат попадать в
    # границы участка, который организации больше не принадлежит.
    await _rebuild_territory(session, organization_id)


# ═══════════════════════════════════════════════════════════════════════════
# Слой карты: кому что принадлежит
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/map/parcels.geojson",
    response_model=GeoJSONFeatureCollection,
    summary="Слой кадастровых участков — кому принадлежит территория",
)
async def parcels_geojson(
    org_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    # Без авторизации: сведения о принадлежности участков — открытые данные
    # ЕГРН, и публичная карта «кому принадлежит берег» это самостоятельная
    # ценность проекта, а не внутренний экран.
    query = (
        select(
            CadastralParcel.id,
            CadastralParcel.cadastral_number,
            CadastralParcel.area_ha,
            Organization.name.label("org_name"),
            Organization.inn.label("org_inn"),
            func.ST_AsGeoJSON(CadastralParcel.geom).label("geojson"),
        )
        .join(Organization, Organization.id == CadastralParcel.organization_id)
        .where(CadastralParcel.geom.isnot(None))
    )
    if org_id is not None:
        query = query.where(CadastralParcel.organization_id == org_id)

    result = await session.execute(query)

    features: list[GeoJSONFeature] = []
    for row in result.all():
        geometry = json.loads(row.geojson)
        features.append(
            GeoJSONFeature(
                geometry=GeoJSONGeometry(
                    type=geometry["type"], coordinates=geometry["coordinates"]
                ),
                properties=GeoJSONProperties(
                    id=row.id,
                    name=row.org_name,
                    description=(
                        f"{row.cadastral_number} · {row.org_name} (ИНН {row.org_inn})"
                        + (f" · {row.area_ha:.1f} га" if row.area_ha else "")
                    ),
                    layer="cadastral_parcel",
                ),
            )
        )

    return GeoJSONFeatureCollection(features=features)
