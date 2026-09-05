"""Анкета волонтёра — образование и прочие поля онбординга."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_session
from app.models import User, UserRole, Volunteer, VolunteerEducation
from app.registry.checksum import is_valid_inn, normalize_inn
from app.registry.providers import RegistryUnavailable
from app.registry.service import InvalidInn, lookup_company
from app.schemas import EducationOut, EducationRequest

router = APIRouter(prefix="/api/v1/volunteers", tags=["volunteers"])


def _require_volunteer(user: User) -> Volunteer:
    if user.role != UserRole.volunteer or user.volunteer is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action is only available for volunteers.",
        )
    return user.volunteer


async def _resolve_institution_name(
    session: AsyncSession,
    inn: str | None,
) -> str | None:
    """Имя из ЕГРЮЛ, если ИНН есть и реестр ответил. Иначе None — не блокируем."""
    if not inn:
        return None
    try:
        info = await lookup_company(session, inn)
    except (InvalidInn, RegistryUnavailable):
        return None
    return info.name if info is not None else None


@router.post(
    "/me/education",
    response_model=EducationOut,
    summary="Сохранить анкету об образовании (идемпотентно)",
)
async def upsert_education(
    body: EducationRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Одна анкета на волонтёра.

    Фронт шлёт форму повторно, пока не получит 2xx — поэтому это upsert,
    а не insert. Вторая запись не появится даже при гонке: volunteer_id
    уникален.
    """
    volunteer = _require_volunteer(user)

    inn: str | None = None
    if body.institution_inn:
        inn = normalize_inn(body.institution_inn)
        if not is_valid_inn(inn):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Некорректный ИНН учреждения.",
            )

    registry_name = await _resolve_institution_name(session, inn)

    existing = await session.scalar(
        select(VolunteerEducation).where(VolunteerEducation.volunteer_id == volunteer.id)
    )
    now = datetime.now(UTC)
    if existing is None:
        existing = VolunteerEducation(volunteer_id=volunteer.id, created_at=now)
        session.add(existing)

    existing.level = body.level
    existing.institution_name = body.institution_name
    existing.institution_inn = inn
    existing.registry_name = registry_name
    existing.grade = body.grade
    existing.city = body.city
    existing.updated_at = now
    await session.flush()

    return EducationOut.model_validate(existing)


@router.get(
    "/me/education",
    response_model=EducationOut,
    summary="Текущая анкета об образовании",
)
async def get_education(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    volunteer = _require_volunteer(user)
    row = await session.scalar(
        select(VolunteerEducation).where(VolunteerEducation.volunteer_id == volunteer.id)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Education not filled.")
    return EducationOut.model_validate(row)
