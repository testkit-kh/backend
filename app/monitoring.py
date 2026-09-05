"""
Площадки многолетних наблюдений и замеры на них.

Зачем отдельно от гипотез: гипотеза — разовый сигнал «здесь мусор», её жизненный
цикл заканчивается вердиктом ООПТ. Площадка живёт годами, и её ценность не в
отдельном замере, а в разнице между замерами — скорости накопления. Проект
«Чистый берег» закладывает такие площадки с 2020 года, и именно эти ряды дают
ответ на вопрос «откуда мусор и сколько его приносит», ради которого проект и
существует.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import EventType, emit
from app.auth import get_current_user
from app.cleanup_cost import estimate_mass_kg, estimate_volume_m3
from app.database import get_session
from app.models import MonitoringSite, SiteSurvey, User, UserRole
from app.schemas import (
    AccumulationInterval,
    MonitoringSiteCreateRequest,
    MonitoringSiteOut,
    SiteAccumulationOut,
    SiteSurveyCreateRequest,
    SiteSurveyOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/monitoring-sites", tags=["monitoring"])

SECONDS_PER_DAY = 86_400.0


def _require_staff_org(user: User) -> uuid.UUID:
    if user.role != UserRole.staff or user.staff is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only ООПТ staff can manage monitoring sites.",
        )
    return user.staff.organization_id


def _site_out(
    site: MonitoringSite,
    surveys_count: int = 0,
    last_surveyed_at=None,
) -> MonitoringSiteOut:
    """Счётчики передаются явно, а не берутся из site.surveys.

    Обращение к relationship здесь означало бы ленивую загрузку: у только что
    созданной площадки она падает в async-контексте (MissingGreenlet), а в
    списке дала бы запрос на каждую строку.
    """
    return MonitoringSiteOut(
        id=site.id,
        organization_id=site.organization_id,
        name=site.name,
        code=site.code,
        area_m2=site.area_m2,
        shoreline_length_m=site.shoreline_length_m,
        established_at=site.established_at,
        protocol=site.protocol,
        is_active=site.is_active,
        created_at=site.created_at,
        surveys_count=surveys_count,
        last_surveyed_at=last_surveyed_at,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Площадки
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "",
    response_model=MonitoringSiteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Заложить площадку многолетних наблюдений (сотрудник ООПТ)",
)
async def create_site(
    body: MonitoringSiteCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    organization_id = _require_staff_org(user)

    existing = await session.execute(select(MonitoringSite).where(MonitoringSite.code == body.code))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Monitoring site with code {body.code} already exists.",
        )

    geom = None
    if body.geometry is not None:
        if body.geometry.type != "Polygon":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Monitoring site geometry must be a Polygon.",
            )
        geom = func.ST_SetSRID(func.ST_GeomFromGeoJSON(body.geometry.model_dump_json()), 4326)

    site = MonitoringSite(
        organization_id=organization_id,
        name=body.name,
        code=body.code,
        geom=geom,
        area_m2=body.area_m2,
        shoreline_length_m=body.shoreline_length_m,
        established_at=body.established_at,
        protocol=body.protocol,
    )
    session.add(site)
    await session.flush()

    await emit(
        session,
        EventType.geo_zone_created,
        user_id=user.id,
        payload={
            "kind": "monitoring_site",
            "site_id": str(site.id),
            "code": site.code,
            "organization_id": str(organization_id),
        },
    )

    return _site_out(site)


@router.get(
    "",
    response_model=list[MonitoringSiteOut],
    summary="Список площадок своей ООПТ",
)
async def list_sites(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    organization_id = _require_staff_org(user)

    # Один запрос с агрегатом вместо обхода relationship по каждой площадке.
    query = (
        select(
            MonitoringSite,
            func.count(SiteSurvey.id).label("surveys_count"),
            func.max(SiteSurvey.surveyed_at).label("last_surveyed_at"),
        )
        .outerjoin(SiteSurvey, SiteSurvey.site_id == MonitoringSite.id)
        .where(MonitoringSite.organization_id == organization_id)
        .group_by(MonitoringSite.id)
        .order_by(MonitoringSite.code)
    )
    result = await session.execute(query)
    return [
        _site_out(site, surveys_count, last_surveyed_at)
        for site, surveys_count, last_surveyed_at in result.unique().all()
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Замеры
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/{site_id}/surveys",
    response_model=SiteSurveyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Записать замер на площадке",
)
async def create_survey(
    site_id: uuid.UUID,
    body: SiteSurveyCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    site = await session.get(MonitoringSite, site_id)
    if site is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Monitoring site not found."
        )

    # Замер может внести и сотрудник ООПТ, и обученный волонтёр — методика
    # одна. Ограничение только для сотрудников чужой территории.
    if user.role == UserRole.staff:
        if user.staff is None or user.staff.organization_id != site.organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only record surveys on your organization's sites.",
            )
    elif user.role == UserRole.volunteer:
        if user.volunteer is None or not user.volunteer.is_trained:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Training must be completed before recording surveys.",
            )

    trash = body.trash
    volume = estimate_volume_m3(
        volume_m3=trash.estimated_volume_m3, area_m2=trash.estimated_area_m2
    )
    mass = (
        estimate_mass_kg(volume, trash.dominant_category)
        if volume is not None and trash.dominant_category is not None
        else None
    )

    survey = SiteSurvey(
        site_id=site.id,
        author_id=user.id,
        surveyed_at=body.surveyed_at,
        trash_categories=(
            [c.value for c in trash.trash_categories] if trash.trash_categories else None
        ),
        dominant_category=trash.dominant_category,
        fraction=trash.fraction,
        item_count=body.item_count,
        estimated_area_m2=trash.estimated_area_m2,
        estimated_volume_m3=trash.estimated_volume_m3,
        computed_volume_m3=round(volume, 3) if volume is not None else None,
        computed_mass_kg=round(mass, 1) if mass is not None else None,
        was_cleaned=body.was_cleaned,
        photo_urls=body.photo_urls,
        notes=body.notes,
    )
    session.add(survey)
    await session.flush()

    await emit(
        session,
        EventType.point_created,
        user_id=user.id,
        payload={
            "mode": "monitoring_survey",
            "site_id": str(site.id),
            "survey_id": str(survey.id),
            "organization_id": str(site.organization_id),
            "item_count": body.item_count,
            "volume_m3": survey.computed_volume_m3,
            "mass_kg": survey.computed_mass_kg,
            "was_cleaned": body.was_cleaned,
        },
    )

    return SiteSurveyOut.model_validate(survey)


@router.get(
    "/{site_id}/surveys",
    response_model=list[SiteSurveyOut],
    summary="Ряд замеров по площадке",
)
async def list_surveys(
    site_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(SiteSurvey).where(SiteSurvey.site_id == site_id).order_by(SiteSurvey.surveyed_at)
    )
    return [SiteSurveyOut.model_validate(s) for s in result.scalars().all()]


@router.get(
    "/{site_id}/accumulation",
    response_model=SiteAccumulationOut,
    summary="Скорость накопления мусора по площадке",
)
async def site_accumulation(
    site_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    site = await session.get(MonitoringSite, site_id)
    if site is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Monitoring site not found."
        )

    result = await session.execute(
        select(SiteSurvey).where(SiteSurvey.site_id == site_id).order_by(SiteSurvey.surveyed_at)
    )
    surveys = list(result.scalars().all())

    intervals: list[AccumulationInterval] = []
    rates: list[float] = []

    for previous, current in zip(surveys, surveys[1:]):
        days = (current.surveyed_at - previous.surveyed_at).total_seconds() / SECONDS_PER_DAY
        if days <= 0:
            continue

        volume_delta: float | None = None
        mass_delta: float | None = None
        kg_per_day: float | None = None
        kg_per_100m_per_day: float | None = None

        if previous.was_cleaned:
            # Площадку убрали — значит текущий замер целиком набежал за
            # интервал. Только в этом случае это честная скорость накопления.
            volume_delta = current.computed_volume_m3
            mass_delta = current.computed_mass_kg
        elif current.computed_volume_m3 is not None and previous.computed_volume_m3 is not None:
            # Без уборки корректна только разность, и то при условии, что
            # мусор не уносило штормом — отрицательную дельту не показываем.
            delta = current.computed_volume_m3 - previous.computed_volume_m3
            volume_delta = delta if delta >= 0 else None
            if current.computed_mass_kg is not None and previous.computed_mass_kg is not None:
                mass = current.computed_mass_kg - previous.computed_mass_kg
                mass_delta = mass if mass >= 0 else None

        if mass_delta is not None:
            kg_per_day = round(mass_delta / days, 3)
            rates.append(kg_per_day)
            if site.shoreline_length_m:
                kg_per_100m_per_day = round(kg_per_day / (site.shoreline_length_m / 100.0), 4)

        intervals.append(
            AccumulationInterval(
                from_surveyed_at=previous.surveyed_at,
                to_surveyed_at=current.surveyed_at,
                days=round(days, 2),
                volume_delta_m3=(round(volume_delta, 3) if volume_delta is not None else None),
                mass_delta_kg=round(mass_delta, 1) if mass_delta is not None else None,
                kg_per_day=kg_per_day,
                kg_per_100m_per_day=kg_per_100m_per_day,
                baseline_cleaned=previous.was_cleaned,
            )
        )

    return SiteAccumulationOut(
        site_id=site.id,
        code=site.code,
        shoreline_length_m=site.shoreline_length_m,
        intervals=intervals,
        mean_kg_per_day=round(sum(rates) / len(rates), 3) if rates else None,
    )
