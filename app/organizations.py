"""
Профиль организации (ООПТ) и её ручная верификация координатором.

``GET/PATCH /organizations/me`` — сотрудник ООПТ смотрит и правит контактные
данные своей организации в личном кабинете. Название и ИНН не редактируются:
они канонические, пришли из ЕГРЮЛ при регистрации (см. ``app/registry``).

``GET /organizations`` / ``POST /organizations/{id}/verify`` — координатор
разбирает организации, которым автоматическая проверка по ИНН не смогла
поставить ``verified`` (сбой ЕГРЮЛ уводит заявку в ``manual_review``, а не в
отказ — до этой ручки её было физически некому разобрать).

Все эндпоинты живут под ``/api/v1`` и требуют JWT. Ролевой контроль —
внутри каждого обработчика, тем же паттерном, что и в остальных роутерах.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_session
from app.hypotheses import _require_staff
from app.models import (
    CadastralParcel,
    MonitoringSite,
    Organization,
    OrgVerificationStatus,
    Staff,
    User,
    UserRole,
)
from app.schemas import (
    OrganizationListItemOut,
    OrganizationProfileOut,
    OrganizationUpdateRequest,
    OrganizationVerifyRequest,
    StaffMemberOut,
)

router = APIRouter(prefix="/api/v1", tags=["organizations"])


def _require_coordinator(user: User) -> None:
    if user.role != UserRole.coordinator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Available to programme coordinators only.",
        )


async def _to_profile_out(session: AsyncSession, org: Organization) -> OrganizationProfileOut:
    staff_rows = await session.execute(
        select(Staff, User)
        .join(User, User.id == Staff.user_id)
        .where(Staff.organization_id == org.id)
    )
    staff_members = [
        StaffMemberOut(id=owner.id, full_name=owner.full_name, email=owner.email)
        for _, owner in staff_rows.all()
    ]
    parcels_count = await session.scalar(
        select(func.count(CadastralParcel.id)).where(CadastralParcel.organization_id == org.id)
    )
    sites_count = await session.scalar(
        select(func.count(MonitoringSite.id)).where(MonitoringSite.organization_id == org.id)
    )

    # Собираем поля явно, а не через model_validate(org): у Organization уже
    # есть ORM-релейшн staff_members (list[Staff], без full_name/email) —
    # автоматическая распаковка по имени поля упала бы на нём.
    return OrganizationProfileOut(
        id=org.id,
        name=org.name,
        inn=org.inn,
        cadastral_number=org.cadastral_number,
        verification_status=org.verification_status,
        created_at=org.created_at,
        contact_email=org.contact_email,
        contact_phone=org.contact_phone,
        description=org.description,
        staff_members=staff_members,
        parcels_count=parcels_count or 0,
        monitoring_sites_count=sites_count or 0,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Кабинет ООПТ
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/organizations/me",
    response_model=OrganizationProfileOut,
    summary="Профиль своей организации (сотрудник ООПТ)",
)
async def get_my_organization(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    staff = _require_staff(user)
    org = await session.get(Organization, staff.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    return await _to_profile_out(session, org)


@router.patch(
    "/organizations/me",
    response_model=OrganizationProfileOut,
    summary="Обновить контактные данные своей организации (сотрудник ООПТ)",
)
async def update_my_organization(
    body: OrganizationUpdateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    staff = _require_staff(user)
    org = await session.get(Organization, staff.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    # Только явно переданные поля: PATCH с одним телефоном не должен
    # обнулить уже заполненное описание.
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(org, field, value)
    await session.flush()

    return await _to_profile_out(session, org)


# ═══════════════════════════════════════════════════════════════════════════
# Верификация координатором
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/organizations",
    response_model=list[OrganizationListItemOut],
    summary="Список организаций для верификации (координатор)",
)
async def list_organizations(
    verification_status: OrgVerificationStatus | None = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_coordinator(user)

    query = select(Organization).order_by(Organization.created_at.desc())
    if verification_status is not None:
        query = query.where(Organization.verification_status == verification_status)

    result = await session.execute(query)
    return [OrganizationListItemOut.model_validate(o) for o in result.scalars().all()]


@router.post(
    "/organizations/{organization_id}/verify",
    response_model=OrganizationListItemOut,
    summary="Ручной вердикт по верификации организации (координатор)",
)
async def verify_organization(
    organization_id: uuid.UUID,
    body: OrganizationVerifyRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_coordinator(user)

    org = await session.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    org.verification_status = (
        OrgVerificationStatus.verified if body.approved else OrgVerificationStatus.failed
    )
    await session.flush()

    return OrganizationListItemOut.model_validate(org)
