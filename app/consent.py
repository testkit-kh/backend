"""
Согласие законного представителя для участников 14–17 лет.

Требование не формальное: приоритетный сегмент проекта — школьники ~14 лет,
и без согласия родителя обработка их персональных данных и участие в полевых
выездах невозможны (152-ФЗ, ст. 9).

Что важно в устройстве флоу: согласие блокирует **карту и выезды**, но не
блокирует **курс**. Подросток может зарегистрироваться и сразу пойти учиться,
пока родитель подписывает бумагу — иначе мы теряем человека на самом узком
месте воронки, ради ожидания документа, который нужен только для следующего
шага.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.age import ADULT_AGE, age_at
from app.auth import get_current_user
from app.database import get_session
from app.models import (
    ConsentStatus,
    Notification,
    NotificationKind,
    ParentalConsent,
    User,
    UserRole,
    Volunteer,
)
from app.schemas import (
    ConsentReviewRequest,
    ParentalConsentCreateRequest,
    ParentalConsentOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["consent"])


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
            detail="Consent moderation is available to programme coordinators only.",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Подача согласия
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/volunteers/me/parental-consent",
    response_model=ParentalConsentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Подать согласие законного представителя (14–17 лет)",
)
async def submit_consent(
    body: ParentalConsentCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    volunteer = _require_volunteer(user)

    if volunteer.birth_date is not None and age_at(volunteer.birth_date) >= ADULT_AGE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Согласие законного представителя нужно только до 18 лет.",
        )
    if volunteer.consent_status == ConsentStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Согласие уже подтверждено.",
        )

    consent = ParentalConsent(
        volunteer_id=volunteer.id,
        representative_name=body.representative_name,
        representative_phone=body.representative_phone,
        representative_email=body.representative_email,
        relation=body.relation,
        scan_url=str(body.scan_url) if body.scan_url else None,
        status=ConsentStatus.awaiting,
    )
    session.add(consent)
    volunteer.consent_status = ConsentStatus.awaiting
    await session.flush()

    return ParentalConsentOut.model_validate(consent)


@router.get(
    "/consents/pending",
    response_model=list[ParentalConsentOut],
    summary="Очередь согласий на проверку (координатор)",
)
async def pending_consents(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_coordinator(user)

    result = await session.execute(
        select(ParentalConsent)
        .where(ParentalConsent.status == ConsentStatus.awaiting)
        .order_by(ParentalConsent.submitted_at)
    )
    return [ParentalConsentOut.model_validate(c) for c in result.unique().scalars().all()]


@router.post(
    "/consents/{consent_id}/review",
    response_model=ParentalConsentOut,
    summary="Проверить согласие (координатор)",
)
async def review_consent(
    consent_id: uuid.UUID,
    body: ConsentReviewRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_coordinator(user)

    consent = await session.get(ParentalConsent, consent_id)
    if consent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consent not found.")
    if consent.status != ConsentStatus.awaiting:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Consent is not awaiting review (current: {consent.status.value}).",
        )

    approved = body.approved
    consent.status = ConsentStatus.approved if approved else ConsentStatus.rejected
    consent.reject_reason = None if approved else body.reason
    consent.reviewed_at = datetime.now(UTC)
    consent.reviewer_id = user.id

    volunteer = await session.get(Volunteer, consent.volunteer_id)
    if volunteer is not None:
        volunteer.consent_status = consent.status
        session.add(
            Notification(
                user_id=volunteer.user_id,
                kind=(
                    NotificationKind.consent_approved
                    if approved
                    else NotificationKind.consent_rejected
                ),
                title=(
                    "Согласие представителя принято"
                    if approved
                    else "Согласие представителя отклонено"
                ),
                body=(
                    "Участие в выездах и работа с картой открыты."
                    if approved
                    else (body.reason or "Проверьте документ и отправьте ещё раз.")
                ),
                action_url="/map" if approved else "/consent",
            )
        )

    await session.flush()
    return ParentalConsentOut.model_validate(consent)
