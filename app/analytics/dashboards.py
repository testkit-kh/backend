"""
Дашборды Metabase внутри приложения и короткая сводка для плашек.

Почему Metabase, а не свои графики: BI поверх той же базы даёт ООПТ живой
инструмент — можно провалиться в цифру, поменять период, выгрузить в Excel,
подписаться на еженедельное письмо. Свой дашборд на графической библиотеке был
бы картинкой, которую нельзя допросить.

Про изоляцию данных. Data sandboxing в Metabase платный, поэтому используется
**static (signed) embedding**: бэкенд подписывает JWT с *заблокированным*
параметром `organization_id`, взятым из токена пользователя. Сотрудник ООПТ не
может увидеть чужие цифры, даже подменив query-параметры в адресной строке —
параметр зашит в подпись, а не в URL.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_session
from app.models import (
    CertificateStatus,
    Hypothesis,
    HypothesisStatus,
    User,
    UserRole,
)
from app.schemas import AnalyticsSummaryOut, DashboardEmbedOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


@dataclass(frozen=True)
class Dashboard:
    """Дашборд в Metabase.

    `number` — его id в Metabase, он появляется только после провижининга
    (`scripts/metabase_seed.py`), поэтому берётся из конфига, а не зашит.
    `scoped` — нужно ли запирать дашборд на организацию: воронка и обучение
    общие по программе, операционка ООПТ — своя у каждой территории.
    """

    slug: str
    title: str
    number: int
    scoped: bool
    #: Роли, которым дашборд вообще показывается.
    roles: tuple[UserRole, ...]


def available_dashboards() -> dict[str, Dashboard]:
    return {
        "funnel": Dashboard(
            slug="funnel",
            title="Воронка и обучение",
            number=settings.METABASE_DASHBOARD_FUNNEL,
            scoped=False,
            roles=(UserRole.coordinator,),
        ),
        "oopt": Dashboard(
            slug="oopt",
            title="Операционка территории",
            number=settings.METABASE_DASHBOARD_OOPT,
            scoped=True,
            roles=(UserRole.staff, UserRole.coordinator),
        ),
        "impact": Dashboard(
            slug="impact",
            title="Экологический эффект",
            number=settings.METABASE_DASHBOARD_IMPACT,
            scoped=True,
            roles=(UserRole.staff, UserRole.coordinator),
        ),
    }


@router.get(
    "/embed/{slug}",
    response_model=DashboardEmbedOut,
    summary="Подписанная ссылка на дашборд Metabase",
)
async def dashboard_embed(
    slug: str,
    user: User = Depends(get_current_user),
):
    dashboards = available_dashboards()
    dashboard = dashboards.get(slug)
    if dashboard is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Дашборд {slug} не найден. Доступны: {', '.join(dashboards)}.",
        )

    if user.role not in dashboard.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Этот дашборд недоступен для вашей роли.",
        )

    if not settings.METABASE_EMBEDDING_SECRET_KEY:
        # 503, а не 500: BI просто не подняли. Фронт по этому коду прячет
        # вкладку, а не показывает ошибку.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metabase не настроен: не задан METABASE_EMBEDDING_SECRET_KEY.",
        )
    if dashboard.number <= 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Дашборд {slug} ещё не создан в Metabase (запустите scripts/metabase_seed.py).",
        )

    params: dict[str, str] = {}
    if dashboard.scoped:
        organization_id = _organization_of(user)
        if organization_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Дашборд территории доступен только сотрудникам ООПТ.",
            )
        # Заблокированный параметр: Metabase не даст переопределить его из
        # URL, поэтому чужие данные недостижимы даже при попытке подмены.
        params["organization_id"] = str(organization_id)

    expires_in = settings.METABASE_EMBED_TOKEN_TTL_MINUTES * 60
    payload = {
        "resource": {"dashboard": dashboard.number},
        "params": params,
        "exp": int(time.time()) + expires_in,
    }
    token = jwt.encode(payload, settings.METABASE_EMBEDDING_SECRET_KEY, algorithm="HS256")

    return DashboardEmbedOut(
        slug=dashboard.slug,
        title=dashboard.title,
        # bordered/titled=false — дашборд встраивается в наш интерфейс и не
        # должен выглядеть как чужое окно внутри окна.
        url=f"{settings.METABASE_SITE_URL}/embed/dashboard/{token}#bordered=false&titled=false",
        expires_in=expires_in,
        scoped_to_organization=dashboard.scoped,
    )


def _organization_of(user: User) -> uuid.UUID | None:
    if user.role == UserRole.staff and user.staff is not None:
        return user.staff.organization_id
    return None


@router.get(
    "/summary",
    response_model=AnalyticsSummaryOut,
    summary="Несколько чисел для плашек в интерфейсе",
)
async def summary(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Сводка для шапки приложения.

    Намеренно не через Metabase: встраивать дашборд ради пяти чисел — это
    лишний iframe и лишняя секунда загрузки. BI отвечает за разбор, а плашки
    отвечают за «что у меня сейчас».
    """
    if user.role == UserRole.staff and user.staff is not None:
        scope = Hypothesis.organization_id == user.staff.organization_id
    elif user.role == UserRole.volunteer:
        scope = Hypothesis.author_id == user.id
    else:
        # Координатор видит программу целиком.
        scope = Hypothesis.id.isnot(None)

    counts = (
        await session.execute(
            select(Hypothesis.status, func.count()).where(scope).group_by(Hypothesis.status)
        )
    ).all()
    by_status = {status_value: count for status_value, count in counts}

    volume, cost = (
        await session.execute(
            select(
                func.coalesce(func.sum(Hypothesis.computed_volume_m3), 0.0),
                func.coalesce(func.sum(Hypothesis.cleanup_cost_rub), 0.0),
            ).where(scope, Hypothesis.status == HypothesisStatus.approved)
        )
    ).one()

    course_status = (
        user.volunteer.certificate_status
        if user.role == UserRole.volunteer and user.volunteer is not None
        else None
    )

    return AnalyticsSummaryOut(
        pending=by_status.get(HypothesisStatus.pending, 0),
        approved=by_status.get(HypothesisStatus.approved, 0),
        rejected=by_status.get(HypothesisStatus.rejected, 0),
        drone_requested=by_status.get(HypothesisStatus.drone_requested, 0),
        confirmed_volume_m3=round(float(volume), 1),
        confirmed_cleanup_cost_rub=round(float(cost), 0),
        certificate_status=course_status or CertificateStatus.none,
    )
