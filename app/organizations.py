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

import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import EventType, emit
from app.auth import get_current_user
from app.config import settings
from app.database import get_session
from app.hypotheses import _require_staff
from app.models import (
    CadastralParcel,
    MonitoringSite,
    Organization,
    OrgVerificationStatus,
    Staff,
    StaffInvite,
    User,
    UserRole,
)
from app.schemas import (
    OrganizationListItemOut,
    OrganizationProfileOut,
    OrganizationUpdateRequest,
    OrganizationVerifyRequest,
    StaffInviteOut,
    StaffMemberOut,
    TerritoryOut,
    TerritoryUpdateRequest,
)

logger = logging.getLogger(__name__)

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
        territory_source=org.territory_source,
        territory_osm_id=org.territory_osm_id,
        has_territory=org.territory_geom is not None,
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


@router.patch(
    "/organizations/me/territory",
    response_model=TerritoryOut,
    summary="Задать границы территории без кадастра (сотрудник ООПТ)",
)
async def update_my_territory(
    body: TerritoryUpdateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Границы из OSM или нарисованные руками.

    Кадастра у части ООПТ нет, ФГИС ЕГРН молчит у другой части — без этой
    ручки сотрудник упирается в пустое поле. source хранится отдельно:
    osm/manual в интерфейсе — ориентир, не выписка из ЕГРН.
    """
    staff = _require_staff(user)
    org = await session.get(Organization, staff.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    geometry = body.geometry.model_dump()
    if geometry["type"] == "Polygon":
        geometry = {"type": "MultiPolygon", "coordinates": [geometry["coordinates"]]}

    org.territory_geom = func.ST_SetSRID(
        func.ST_GeomFromGeoJSON(json.dumps(geometry)),
        4326,
    )
    org.territory_source = body.source
    org.territory_osm_id = body.osm_id
    if body.name:
        # Имя из OSM не затирает каноническое из ЕГРЮЛ: то — юридическое.
        pass
    await session.flush()

    await emit(
        session,
        EventType.geo_zone_created,
        user_id=user.id,
        payload={
            "kind": "organization_territory",
            "source": body.source,
            "osm_id": body.osm_id,
            "organization_id": str(org.id),
            "name": body.name,
        },
    )

    return TerritoryOut(
        organization_id=org.id,
        source=body.source,
        osm_id=body.osm_id,
        name=body.name or org.name,
        has_territory=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Инвайты сотрудников (P1-6)
# ═══════════════════════════════════════════════════════════════════════════

# Алфавит кода без 0/O/1/I/L: код диктуют по телефону и переписывают руками,
# а различить эти символы на слух и в рукописи нельзя.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_GROUPS = 3
_CODE_GROUP_LEN = 4


def _generate_invite_code() -> str:
    """Код вида K7QF-9WMR-2XTB.

    31 символ в алфавите, 12 знаков — около 59 бит: угадать нельзя, а
    продиктовать можно. Группами по четыре, потому что такой код человек
    переносит без ошибок, а сплошную строку — нет.
    """
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_GROUPS * _CODE_GROUP_LEN))
    return "-".join(body[i : i + _CODE_GROUP_LEN] for i in range(0, len(body), _CODE_GROUP_LEN))


@router.post(
    "/organizations/me/invites",
    response_model=StaffInviteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Выдать одноразовый код регистрации коллеги (сотрудник ООПТ)",
)
async def create_staff_invite(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Выдать инвайт в свою ООПТ.

    Организация берётся из привязки сотрудника, а не из запроса: пригласить в
    чужую ООПТ невозможно не по проверке, а по устройству ручки — подставить
    id просто некуда.

    Код возвращается в этом ответе один раз и больше нигде не показывается.
    """
    staff = _require_staff(user)

    # Коллизия при 59 битах невероятна, но unique-констрейнт на code
    # превратил бы её в 500. Несколько попыток дешевле, чем объяснять
    # пользователю случайную ошибку.
    for attempt in range(5):
        code = _generate_invite_code()
        clash = await session.scalar(select(StaffInvite.id).where(StaffInvite.code == code))
        if clash is None:
            break
        logger.warning("Коллизия кода инвайта (попытка %d)", attempt + 1)
    else:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not generate an invite code, please retry.",
        )

    invite = StaffInvite(
        organization_id=staff.organization_id,
        code=code,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.STAFF_INVITE_TTL_HOURS),
        created_by_id=user.id,
    )
    session.add(invite)
    await session.flush()

    # Выдачу доступа логируем всегда: это то событие, которое разбирают,
    # когда в организации обнаруживается лишний аккаунт.
    logger.info(
        "Сотрудник %s выдал инвайт %s в ООПТ %s",
        user.id,
        invite.id,
        staff.organization_id,
    )

    return StaffInviteOut.model_validate(invite)


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
