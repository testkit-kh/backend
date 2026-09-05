"""
Обучение: уход на курс, сертификат, модерация, доступ к карте.

Ключевое изменение против прежнего поведения: загрузка сертификата больше **не**
открывает карту. Раньше `is_trained` выставлялся по факту присланной ссылки, то
есть проверки не было вовсе, а KPI «% завершивших курс» считался по числу
вставленных в поле URL. Теперь между подачей и доступом стоит человек, и в
событийную шину попадают обе точки: `certificate_uploaded` и
`certificate_verified(approved|rejected)`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import EventType, emit
from app.auth import get_current_user
from app.certificates import issue_for_volunteer
from app.config import settings
from app.database import get_session
from app.models import (
    CertificateStatus,
    Notification,
    NotificationKind,
    User,
    UserRole,
    Volunteer,
)
from app.schemas import (
    CertificateRequest,
    CertificateReviewRequest,
    CourseRedirectOut,
    CourseStatusOut,
    PendingCertificateOut,
    VolunteerProfileOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["course"])


def _require_volunteer(user: User) -> Volunteer:
    if user.role != UserRole.volunteer or user.volunteer is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action is only available for volunteers.",
        )
    return user.volunteer


def _require_coordinator(user: User) -> None:
    if user.role != UserRole.coordinator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Certificate moderation is available to programme coordinators only.",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Уход на курс
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/course/redirect",
    response_model=CourseRedirectOut,
    summary="Уйти на курс «Школы Защитников Природы»",
)
async def course_redirect(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    nid: uuid.UUID | None = Query(
        default=None,
        description="Id уведомления, если переход пришёл из напоминания",
    ),
):
    volunteer = _require_volunteer(user)

    # Первый уход фиксируем отдельно: return rate считается от него, и
    # повторные заходы не должны его сдвигать.
    if volunteer.course_redirect_at is None:
        volunteer.course_redirect_at = datetime.now(UTC)

    if nid is not None:
        notification = await session.get(Notification, nid)
        if notification is not None and notification.user_id == user.id:
            if notification.clicked_at is None:
                notification.clicked_at = datetime.now(UTC)
            notification.read_at = notification.read_at or datetime.now(UTC)
            # Без этого события KPI «эффективность напоминаний» посчитать
            # нечем: органический возврат и возврат по напоминанию неотличимы.
            await emit(
                session,
                EventType.reminder_clicked,
                user_id=user.id,
                payload={
                    "notification_id": str(nid),
                    "kind": notification.kind.value,
                },
            )

    await emit(
        session,
        EventType.course_redirect_click,
        user_id=user.id,
        payload={
            "first_time": volunteer.course_redirect_at is not None,
            "from_notification": str(nid) if nid else None,
        },
    )

    return CourseRedirectOut(url=settings.COURSE_SIGNUP_URL)


@router.get(
    "/course/me",
    response_model=CourseStatusOut,
    summary="Где я на пути обучения",
)
async def course_status(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    volunteer = _require_volunteer(user)

    # Возврат в приложение после ухода на курс — событие из KPI-документа,
    # заведённое отдельно от сертификата: человек может зайти «посмотреть
    # прогресс», ещё не закончив обучение, и это ценный сигнал.
    if (
        volunteer.course_redirect_at is not None
        and volunteer.certificate_status == CertificateStatus.none
    ):
        await emit(
            session,
            EventType.app_reopened_post_redirect,
            user_id=user.id,
            payload={
                "days_since_redirect": (datetime.now(UTC) - volunteer.course_redirect_at).days
            },
        )

    return CourseStatusOut(
        course_url=settings.COURSE_SIGNUP_URL,
        certificate_status=volunteer.certificate_status,
        certificate_url=volunteer.certificate_url,
        certificate_submitted_at=volunteer.certificate_submitted_at,
        certificate_reviewed_at=volunteer.certificate_reviewed_at,
        certificate_reject_reason=volunteer.certificate_reject_reason,
        course_redirect_at=volunteer.course_redirect_at,
        map_access_granted_at=volunteer.map_access_granted_at,
        has_map_access=volunteer.is_trained,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Сертификат
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/volunteers/me/certificate",
    response_model=VolunteerProfileOut,
    summary="Отправить сертификат на проверку",
)
async def submit_certificate(
    body: CertificateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    volunteer = _require_volunteer(user)

    if volunteer.certificate_status == CertificateStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Certificate is already approved.",
        )

    volunteer.certificate_url = str(body.certificate_url)
    volunteer.certificate_status = CertificateStatus.pending
    volunteer.certificate_submitted_at = datetime.now(UTC)
    # Повторная подача после отказа: старую причину убираем, иначе волонтёр
    # будет видеть отказ по уже исправленному сертификату.
    volunteer.certificate_reject_reason = None
    volunteer.certificate_reviewed_at = None
    volunteer.certificate_reviewer_id = None
    await session.flush()

    await emit(
        session,
        EventType.certificate_uploaded,
        user_id=user.id,
        payload={"kind": "url"},
    )

    return VolunteerProfileOut.model_validate(volunteer)


@router.get(
    "/certificates/pending",
    response_model=list[PendingCertificateOut],
    summary="Очередь сертификатов на проверку (координатор)",
)
async def pending_certificates(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_coordinator(user)

    query = (
        select(Volunteer, User)
        .join(User, User.id == Volunteer.user_id)
        .where(Volunteer.certificate_status == CertificateStatus.pending)
        .order_by(Volunteer.certificate_submitted_at)
    )
    result = await session.execute(query)

    return [
        PendingCertificateOut(
            volunteer_id=volunteer.id,
            user_id=owner.id,
            full_name=owner.full_name,
            email=owner.email,
            certificate_url=volunteer.certificate_url,
            certificate_submitted_at=volunteer.certificate_submitted_at,
            course_redirect_at=volunteer.course_redirect_at,
        )
        for volunteer, owner in result.unique().all()
    ]


@router.post(
    "/certificates/{volunteer_id}/review",
    response_model=VolunteerProfileOut,
    summary="Проверить сертификат: принять или отклонить (координатор)",
)
async def review_certificate(
    volunteer_id: uuid.UUID,
    body: CertificateReviewRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_coordinator(user)

    volunteer = await session.get(Volunteer, volunteer_id)
    if volunteer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Volunteer not found.")
    if volunteer.certificate_status != CertificateStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Certificate is not pending review "
            f"(current status: {volunteer.certificate_status.value}).",
        )

    now = datetime.now(UTC)
    approved = body.approved

    volunteer.certificate_status = (
        CertificateStatus.approved if approved else CertificateStatus.rejected
    )
    volunteer.certificate_reviewed_at = now
    volunteer.certificate_reviewer_id = user.id
    volunteer.certificate_reject_reason = None if approved else body.reason

    # Время от подачи до вердикта — узкое место воронки: пока сертификат
    # лежит в очереди, волонтёр не может поставить ни одной точки.
    time_to_review = (
        (now - volunteer.certificate_submitted_at).total_seconds()
        if volunteer.certificate_submitted_at
        else None
    )

    await emit(
        session,
        EventType.certificate_verified,
        user_id=volunteer.user_id,
        payload={
            "method": "manual",
            "status": "approved" if approved else "rejected",
            "reviewer_id": str(user.id),
            "reason": None if approved else body.reason,
            "time_to_review": time_to_review,
        },
    )

    if approved:
        volunteer.is_trained = True
        volunteer.map_access_granted_at = now
        await emit(session, EventType.map_access_granted, user_id=volunteer.user_id)
        owner = await session.get(User, volunteer.user_id)
        if owner is not None:
            await issue_for_volunteer(session, volunteer, full_name=owner.full_name)

    session.add(
        Notification(
            user_id=volunteer.user_id,
            kind=(
                NotificationKind.certificate_approved
                if approved
                else NotificationKind.certificate_rejected
            ),
            title=("Сертификат принят — карта открыта" if approved else "Сертификат отклонён"),
            body=(
                "Можно отмечать загрязнения на карте своей территории."
                if approved
                else (body.reason or "Проверьте сертификат и отправьте ещё раз.")
            ),
            action_url="/map" if approved else "/course",
            payload={"reviewer_id": str(user.id)},
        )
    )
    await session.flush()

    return VolunteerProfileOut.model_validate(volunteer)
