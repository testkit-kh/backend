"""
Business-logic router — hypotheses, certificate, map layers.

All endpoints live under ``/api/v1`` and require a valid JWT.
Role-based access is enforced per-handler (volunteer / staff / any).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from geoalchemy2.functions import ST_AsGeoJSON, ST_Contains, ST_MakePoint, ST_SetSRID
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.age import has_field_access
from app.analytics import EventType, emit
from app.auth import get_current_user
from app.cleanup_cost import estimate_cleanup
from app.database import get_session
from app.models import (
    CertificateStatus,
    Event,
    EventStatus,
    Hypothesis,
    HypothesisStatus,
    MonitoringSite,
    Organization,
    Staff,
    User,
    UserRole,
    Volunteer,
)
from app.schemas import (
    GeoJSONFeature,
    GeoJSONFeatureCollection,
    GeoJSONGeometry,
    GeoJSONProperties,
    HypothesisCreateRequest,
    HypothesisOut,
    HypothesisValidateRequest,
    HypothesisValidateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["business-logic"])


# ═══════════════════════════════════════════════════════════════════════════
# Helper — role guards
# ═══════════════════════════════════════════════════════════════════════════

def _require_volunteer(user: User) -> Volunteer:
    """Return the Volunteer profile or raise 403."""
    if user.role != UserRole.volunteer or user.volunteer is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action is only available for volunteers.",
        )
    return user.volunteer


def _require_staff(user: User) -> Staff:
    """Return the Staff profile or raise 403."""
    if user.role != UserRole.staff or user.staff is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action is only available for staff members.",
        )
    return user.staff


# ═══════════════════════════════════════════════════════════════════════════
# 1. POST /api/v1/hypotheses — create a hypothesis (volunteer only)
# ═══════════════════════════════════════════════════════════════════════════

@router.post(
    "/hypotheses",
    response_model=HypothesisOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new ecological observation (hypothesis)",
)
async def create_hypothesis(
    body: HypothesisCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vol = _require_volunteer(user)

    # Два независимых условия. Согласие представителя проверяется отдельно от
    # обучения: подросток может пройти курс, пока родитель подписывает бумагу,
    # но в поле без документа не выходит.
    if not has_field_access(vol):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Нужно согласие законного представителя: участникам до 18 лет "
                   "работа с картой открывается после его подтверждения.",
        )

    # Доступ к карте даётся не за факт присланного сертификата, а за
    # подтверждённый координатором. См. app/course.py.
    if vol.certificate_status != CertificateStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Карта открывается после проверки сертификата о прохождении "
                   "курса. Текущий статус: "
                   f"{vol.certificate_status.value}.",
        )

    # --- Spatial lookup: which ООПТ polygon contains this point? -----------
    # Build a PostGIS POINT from (lon, lat) — note the order: x=lon, y=lat.
    point = ST_SetSRID(ST_MakePoint(body.lon, body.lat), 4326)

    org_query = (
        select(Organization.id)
        .where(
            Organization.territory_geom.isnot(None),
            ST_Contains(Organization.territory_geom, point),
        )
        .limit(1)
    )
    result = await session.execute(org_query)
    org_id: uuid.UUID | None = result.scalar_one_or_none()

    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No organization territory contains the given coordinates. "
                   "Please check lat/lon values.",
        )

    # --- Проверка площадки многолетних наблюдений ---------------------------
    if body.monitoring_site_id is not None:
        site = await session.get(MonitoringSite, body.monitoring_site_id)
        if site is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Monitoring site not found.",
            )
        if site.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Monitoring site belongs to a different territory "
                       "than the point coordinates.",
            )

    # --- Смета уборки -------------------------------------------------------
    # Считается только если человек указал и объём (или площадь), и
    # преобладающий тип, и способ доступа. Иначе поля остаются пустыми:
    # нулевая смета хуже отсутствующей, потому что попадёт в суммы по ООПТ.
    trash = body.trash
    estimate = None
    if trash.dominant_category is not None and trash.access_type is not None:
        estimate = estimate_cleanup(
            volume_m3=trash.estimated_volume_m3,
            area_m2=trash.estimated_area_m2,
            dominant=trash.dominant_category,
            access=trash.access_type,
        )

    # --- Create hypothesis --------------------------------------------------
    hypothesis = Hypothesis(
        author_id=user.id,
        organization_id=org_id,
        lat=body.lat,
        lon=body.lon,
        location=func.ST_SetSRID(func.ST_MakePoint(body.lon, body.lat), 4326),
        description=body.description,
        photo_url=body.photo_url,
        status=HypothesisStatus.pending,
        trash_categories=(
            [c.value for c in trash.trash_categories]
            if trash.trash_categories
            else None
        ),
        dominant_category=trash.dominant_category,
        fraction=trash.fraction,
        access_type=trash.access_type,
        estimated_area_m2=trash.estimated_area_m2,
        estimated_volume_m3=trash.estimated_volume_m3,
        computed_volume_m3=estimate.volume_m3 if estimate else None,
        computed_mass_kg=estimate.mass_kg if estimate else None,
        cleanup_cost_rub=estimate.total_rub if estimate else None,
        cost_assumptions=estimate.assumptions if estimate else None,
        monitoring_site_id=body.monitoring_site_id,
    )
    session.add(hypothesis)
    await session.flush()

    await emit(
        session,
        EventType.point_created,
        user_id=user.id,
        lat=body.lat,
        lon=body.lon,
        payload={
            "hypothesis_id": str(hypothesis.id),
            "organization_id": str(org_id),
            "mode": "online",
            "has_photo": body.photo_url is not None,
            # Состав и объём попадают в событие, а не только в таблицу: KPI
            # «сколько мусора найдено» и «сколько это стоит убрать» считаются
            # по событийной шине, как и вся остальная аналитика.
            "trash_categories": (
                [c.value for c in trash.trash_categories]
                if trash.trash_categories
                else None
            ),
            "dominant_category": (
                trash.dominant_category.value if trash.dominant_category else None
            ),
            "fraction": trash.fraction.value if trash.fraction else None,
            "access_type": trash.access_type.value if trash.access_type else None,
            "volume_m3": estimate.volume_m3 if estimate else None,
            "mass_kg": estimate.mass_kg if estimate else None,
            "cleanup_cost_rub": estimate.total_rub if estimate else None,
            "monitoring_site_id": (
                str(body.monitoring_site_id) if body.monitoring_site_id else None
            ),
        },
    )
    # Mirror event on the ООПТ side of the funnel: the same fact, but it is
    # what the "active ООПТ" and "points in my zone" metrics count.
    await emit(
        session,
        EventType.point_received_in_zone,
        user_id=user.id,
        lat=body.lat,
        lon=body.lon,
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
    result = await session.execute(
        select(Hypothesis).where(Hypothesis.id == hypothesis_id)
    )
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
    time_to_validate = (
        datetime.now(UTC) - hypothesis.created_at
    ).total_seconds()

    # Update status
    hypothesis.status = body.status
    await session.flush()

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

    # --- Layer 2: approved hypothesis points --------------------------------
    hyp_query = select(
        Hypothesis.id,
        Hypothesis.lat,
        Hypothesis.lon,
        Hypothesis.description,
        Hypothesis.status,
    ).where(Hypothesis.status == HypothesisStatus.approved)

    hyp_result = await session.execute(hyp_query)
    for row in hyp_result.all():
        features.append(
            GeoJSONFeature(
                geometry=GeoJSONGeometry(
                    type="Point",
                    coordinates=[row.lon, row.lat],
                ),
                properties=GeoJSONProperties(
                    id=row.id,
                    description=row.description,
                    status=row.status.value,
                    layer="approved_hypothesis",
                ),
            )
        )

    return GeoJSONFeatureCollection(features=features)
