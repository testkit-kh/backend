"""
Business-logic router — hypotheses, certificate, map layers.

All endpoints live under ``/api/v1`` and require a valid JWT.
Role-based access is enforced per-handler (volunteer / staff / any).
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from geoalchemy2.functions import ST_AsGeoJSON, ST_Contains, ST_MakePoint, ST_SetSRID
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_session
from app.models import (
    Event,
    EventStatus,
    Hypothesis,
    HypothesisStatus,
    Organization,
    Staff,
    User,
    UserRole,
    Volunteer,
)
from app.schemas import (
    CertificateRequest,
    GeoJSONFeature,
    GeoJSONFeatureCollection,
    GeoJSONGeometry,
    GeoJSONProperties,
    HypothesisCreateRequest,
    HypothesisOut,
    HypothesisValidateRequest,
    HypothesisValidateResponse,
    VolunteerProfileOut,
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

    # Business rule: volunteer must have completed training
    if not vol.is_trained:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must complete training before submitting hypotheses. "
                   "Upload your Stepik certificate at POST /api/v1/volunteers/me/certificate.",
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
    )
    session.add(hypothesis)
    await session.flush()

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

    # Update status
    hypothesis.status = body.status
    await session.flush()

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

    return HypothesisValidateResponse(
        hypothesis=HypothesisOut.model_validate(hypothesis),
        event_id=event_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4. POST /api/v1/volunteers/me/certificate — submit training cert
# ═══════════════════════════════════════════════════════════════════════════

@router.post(
    "/volunteers/me/certificate",
    response_model=VolunteerProfileOut,
    summary="Submit a Stepik certificate to confirm volunteer training",
)
async def submit_certificate(
    body: CertificateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vol = _require_volunteer(user)

    vol.stepik_cert_url = str(body.stepik_cert_url)
    vol.is_trained = True
    await session.flush()

    return VolunteerProfileOut.model_validate(vol)


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
