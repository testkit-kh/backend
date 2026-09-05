"""
Auth router — registration, login, profile.

Business rules
--------------
* Volunteers must be ≥ 14 y.o. (is_over_14 == True).
* INN verification is delegated to an external service mock.
  If the mock raises / returns an error the org is NOT rejected —
  it gets `verification_status = manual_review` so a human can check later.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.age import MIN_AGE, age_at, required_consent_status
from app.analytics import EventType, emit
from app.config import settings
from app.database import get_session
from app.models import (
    ConsentStatus,
    Organization,
    OrgVerificationStatus,
    Staff,
    User,
    UserRole,
    Volunteer,
)
from app.registry import InvalidInn, RegistryUnavailable, lookup_company
from app.schemas import (
    CoordinatorProfile,
    OrganizationOut,
    OrganizationRegisterRequest,
    StaffProfile,
    TokenResponse,
    VolunteerProfile,
    VolunteerRegisterRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# OAuth2 scheme — provides the "Authorize" button in Swagger UI
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# ---------------------------------------------------------------------------
# Password hashing  (bcrypt directly — passlib is broken with bcrypt ≥ 4.1)
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def create_access_token(data: dict) -> str:
    payload = data.copy()
    expire = datetime.now(UTC) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload.update({"exp": expire})
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


# ---------------------------------------------------------------------------
# External INN verification — MOCK / STUB
# ---------------------------------------------------------------------------


async def verify_inn_external(session: AsyncSession, inn: str) -> tuple[bool, str | None]:
    """Проверка ИНН по ЕГРЮЛ.

    Возвращает (действующая ли организация, её наименование).
    Бросает RegistryUnavailable, если источник не ответил — вызывающий код
    переводит заявку в ручную модерацию, а не отказывает пользователю.

    Контрольная сумма проверяется до сети (`app/registry/checksum.py`):
    опечатку нет смысла нести в ЕГРЮЛ.
    """
    try:
        info = await lookup_company(session, inn)
    except InvalidInn:
        return False, None

    if info is None:
        return False, None
    return info.is_active, info.name


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Decode JWT, fetch the User from DB, return it (or raise 401)."""
    payload = decode_access_token(token)
    user_id: str | None = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload missing 'sub'",
        )
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user id in token",
        )

    result = await session.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/register/volunteer",
    response_model=VolunteerProfile,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new volunteer",
)
async def register_volunteer(
    body: VolunteerRegisterRequest,
    session: AsyncSession = Depends(get_session),
):
    # Возраст: 14 — нижняя граница самостоятельного участия, до 18 нужно
    # согласие законного представителя. Если дата рождения не прислана,
    # опираемся на флаг — старый клиент шлёт только его.
    if body.birth_date is not None:
        age = age_at(body.birth_date)
        if age < MIN_AGE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Самостоятельное участие — с {MIN_AGE} лет. "
                "Для младших участников есть формат со школой.",
            )
        is_over_14 = True
        consent_status = required_consent_status(body.birth_date)
    else:
        if not body.is_over_14:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Volunteers must be at least 14 years old (is_over_14 must be true).",
            )
        is_over_14 = True
        consent_status = ConsentStatus.not_required

    # Check duplicate email
    existing = await session.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists.",
        )

    user = User(
        email=body.email,
        full_name=body.full_name,
        password_hash=hash_password(body.password),
        role=UserRole.volunteer,
    )
    session.add(user)
    await session.flush()  # get user.id

    volunteer = Volunteer(
        user_id=user.id,
        is_over_14=is_over_14,
        birth_date=body.birth_date,
        consent_status=consent_status,
        is_trained=False,
    )
    session.add(volunteer)
    await session.flush()

    await emit(
        session,
        EventType.user_registered,
        user_id=user.id,
        payload={
            "role": UserRole.volunteer.value,
            "source": body.source or "direct",
            "referred_by": str(body.referred_by) if body.referred_by else None,
            "is_over_14": is_over_14,
            "age": age_at(body.birth_date) if body.birth_date else None,
            "requires_consent": consent_status == ConsentStatus.awaiting,
        },
    )

    return VolunteerProfile(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        created_at=user.created_at,
        is_trained=volunteer.is_trained,
        is_over_14=volunteer.is_over_14,
        birth_date=volunteer.birth_date,
        consent_status=volunteer.consent_status,
        certificate_status=volunteer.certificate_status,
        certificate_url=volunteer.certificate_url,
    )


@router.post(
    "/register/organization",
    response_model=StaffProfile,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new organization with its first admin staff member",
)
async def register_organization(
    body: OrganizationRegisterRequest,
    session: AsyncSession = Depends(get_session),
):
    # Check duplicate email
    existing_user = await session.execute(select(User).where(User.email == body.email))
    if existing_user.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists.",
        )

    # Check duplicate INN
    existing_org = await session.execute(select(Organization).where(Organization.inn == body.inn))
    if existing_org.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An organization with this INN already exists.",
        )

    # ---- External INN verification (with graceful fallback) ---------------
    verification_status = OrgVerificationStatus.pending
    registry_name: str | None = None
    try:
        is_valid, registry_name = await verify_inn_external(session, body.inn)
        verification_status = (
            OrgVerificationStatus.verified if is_valid else OrgVerificationStatus.failed
        )
    except RegistryUnavailable:
        # External API is down — do NOT fail the whole registration.
        # Mark for manual review instead.
        logger.warning(
            "External INN verification failed for INN=%s; marking organization for manual review.",
            body.inn,
        )
        verification_status = OrgVerificationStatus.manual_review

    # Наименование из ЕГРЮЛ приоритетнее введённого руками: в реестре оно
    # каноническое, а пользователь напишет «Кроноцкий» вместо полного.
    org = Organization(
        name=registry_name or body.org_name,
        inn=body.inn,
        cadastral_number=body.cadastral_number,
        verification_status=verification_status,
    )
    session.add(org)
    await session.flush()

    # Create admin-staff user
    user = User(
        email=body.email,
        full_name=body.full_name,
        password_hash=hash_password(body.password),
        role=UserRole.staff,
    )
    session.add(user)
    await session.flush()

    staff = Staff(
        user_id=user.id,
        organization_id=org.id,
    )
    session.add(staff)
    await session.flush()

    await emit(
        session,
        EventType.oopt_registered,
        user_id=user.id,
        payload={
            "organization_id": str(org.id),
            "inn": body.inn,
            "cadastral_number": body.cadastral_number,
        },
    )
    # Recorded separately from the registration itself: the KPI document tracks
    # the auto_ok / auto_fail_manual_queue split as its own metric.
    await emit(
        session,
        EventType.inn_verification,
        user_id=user.id,
        payload={
            "organization_id": str(org.id),
            "status": (
                "auto_ok"
                if verification_status == OrgVerificationStatus.verified
                else "auto_fail_manual_queue"
            ),
            "verification_status": verification_status.value,
        },
    )

    return StaffProfile(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        created_at=user.created_at,
        organization=OrganizationOut.model_validate(org),
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate and receive a JWT access token",
)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).where(User.email == form.username))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(form.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    token_data: dict = {
        "sub": str(user.id),
        "role": user.role.value,
    }

    # For staff users, embed organization_id into the token payload
    if user.role == UserRole.staff and user.staff is not None:
        token_data["organization_id"] = str(user.staff.organization_id)

    access_token = create_access_token(token_data)
    return TokenResponse(access_token=access_token)


@router.get(
    "/me",
    response_model=VolunteerProfile | StaffProfile | CoordinatorProfile,
    summary="Get current user profile (role-dependent)",
)
async def get_me(
    current_user: User = Depends(get_current_user),
):
    if current_user.role == UserRole.volunteer:
        vol = current_user.volunteer
        if vol is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Volunteer profile missing for user.",
            )
        return VolunteerProfile(
            id=current_user.id,
            email=current_user.email,
            full_name=current_user.full_name,
            role=current_user.role,
            created_at=current_user.created_at,
            is_trained=vol.is_trained,
            is_over_14=vol.is_over_14,
            birth_date=vol.birth_date,
            consent_status=vol.consent_status,
            certificate_status=vol.certificate_status,
            certificate_url=vol.certificate_url,
        )

    if current_user.role == UserRole.staff:
        st = current_user.staff
        if st is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Staff profile missing for user.",
            )
        # Eagerly loaded via relationship, but access org explicitly
        return StaffProfile(
            id=current_user.id,
            email=current_user.email,
            full_name=current_user.full_name,
            role=current_user.role,
            created_at=current_user.created_at,
            organization=OrganizationOut.model_validate(st.organization),
        )

    if current_user.role == UserRole.coordinator:
        return CoordinatorProfile(
            id=current_user.id,
            email=current_user.email,
            full_name=current_user.full_name,
            role=current_user.role,
            created_at=current_user.created_at,
        )

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Unknown role.",
    )
