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
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import refresh as refresh_tokens
from app.age import MIN_AGE, age_at, required_consent_status
from app.analytics import EventType, emit
from app.config import settings
from app.database import get_session
from app.models import (
    ConsentStatus,
    Organization,
    OrgVerificationStatus,
    ParentalConsent,
    Staff,
    StaffInvite,
    User,
    UserRole,
    Volunteer,
)
from app.registry import InvalidInn, RegistryUnavailable, lookup_company
from app.schemas import (
    CoordinatorProfile,
    CoordinatorRegisterRequest,
    OrganizationOut,
    OrganizationRegisterRequest,
    ParentalConsentOut,
    StaffProfile,
    StaffRegisterRequest,
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


async def _latest_consent(
    session: AsyncSession,
    volunteer_id: uuid.UUID,
) -> ParentalConsent | None:
    return await session.scalar(
        select(ParentalConsent)
        .where(ParentalConsent.volunteer_id == volunteer_id)
        .order_by(ParentalConsent.submitted_at.desc())
        .limit(1)
    )


def _organization_out(org: Organization) -> OrganizationOut:
    out = OrganizationOut.model_validate(org)
    out.has_territory = org.territory_geom is not None
    return out


async def _volunteer_profile(
    session: AsyncSession,
    user: User,
    volunteer: Volunteer,
) -> VolunteerProfile:
    latest = await _latest_consent(session, volunteer.id)
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
        latest_consent=(ParentalConsentOut.model_validate(latest) if latest is not None else None),
    )


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

    return await _volunteer_profile(session, user, volunteer)


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
        organization=_organization_out(org),
    )


@router.post(
    "/register/staff",
    response_model=StaffProfile,
    status_code=status.HTTP_201_CREATED,
    summary="Register a staff member using a one-time invite code",
)
async def register_staff(
    body: StaffRegisterRequest,
    session: AsyncSession = Depends(get_session),
):
    """P1-6: регистрация сотрудника по инвайту.

    Ручка открытая, но публичной регистрацией не является: без действующего
    кода она ничего не создаёт. Организация берётся из инвайта, а не из
    запроса.
    """
    # FOR UPDATE — против гонки: без блокировки два одновременных запроса с
    # одним кодом оба увидели бы used_at = NULL, и одноразовый код впустил бы
    # двоих. Строка держится до конца транзакции, второй запрос дождётся и
    # увидит код уже использованным.
    invite = await session.scalar(
        select(StaffInvite).where(StaffInvite.code == body.invite_code).with_for_update()
    )

    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite code not found.",
        )
    # Сначала «использован», потом «истёк»: использованный код важнее
    # диагностически, и сообщение про срок на нём сбивало бы с толку.
    if invite.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This invite code has already been used.",
        )
    if invite.expires_at <= datetime.now(UTC):
        # 410, а не 404: код существовал, и по ответу это видно. Приглашённый
        # должен понять, что нужно попросить новый, а не искать опечатку.
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This invite code has expired — please request a new one.",
        )

    organization = await session.get(Organization, invite.organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization from the invite no longer exists.",
        )

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
        role=UserRole.staff,
    )
    session.add(user)
    await session.flush()

    staff = Staff(user_id=user.id, organization_id=invite.organization_id)
    session.add(staff)

    # Гасим код в той же транзакции, что создаёт аккаунт: иначе при сбое
    # после создания пользователя код остался бы живым и впустил второго.
    invite.used_at = datetime.now(UTC)
    invite.used_by_id = user.id
    await session.flush()

    await emit(
        session,
        EventType.user_registered,
        user_id=user.id,
        payload={
            "role": UserRole.staff.value,
            "source": "staff_invite",
            "organization_id": str(invite.organization_id),
            "invite_id": str(invite.id),
            "invited_by": str(invite.created_by_id) if invite.created_by_id else None,
        },
    )

    return StaffProfile(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        created_at=user.created_at,
        organization=_organization_out(organization),
    )


@router.post(
    "/register/coordinator",
    response_model=CoordinatorProfile,
    status_code=status.HTTP_201_CREATED,
    summary="Register a programme coordinator using the invite code",
)
async def register_coordinator(
    body: CoordinatorRegisterRequest,
    session: AsyncSession = Depends(get_session),
):
    """Координатор не привязан к ООПТ и видит все очереди модерации.

    Код один на программу и живёт в COORDINATOR_INVITE_CODE. Пустой код —
    ручка выключена: иначе это была бы открытая регистрация суперпользователя.
    """
    expected = settings.COORDINATOR_INVITE_CODE.strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Coordinator registration is not configured.",
        )
    given = body.invite_code.strip()
    if len(given) != len(expected) or not secrets.compare_digest(given, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid coordinator invite code.",
        )

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
        role=UserRole.coordinator,
    )
    session.add(user)
    await session.flush()

    await emit(
        session,
        EventType.user_registered,
        user_id=user.id,
        payload={"role": UserRole.coordinator.value, "source": "coordinator_invite"},
    )

    return CoordinatorProfile(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        created_at=user.created_at,
    )


def _access_token_for(user: User) -> str:
    """Собрать access-токен с ролью и, для сотрудника, его ООПТ."""
    token_data: dict = {
        "sub": str(user.id),
        "role": user.role.value,
    }
    # For staff users, embed organization_id into the token payload
    if user.role == UserRole.staff and user.staff is not None:
        token_data["organization_id"] = str(user.staff.organization_id)
    return create_access_token(token_data)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate and receive a JWT access token",
)
async def login(
    response: Response,
    request: Request,
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

    # P1-7: вход — единственное место, где сессия начинается. Без выдачи
    # refresh-куки здесь ручке /auth/refresh было бы нечего читать.
    raw_refresh, _ = await refresh_tokens.issue(
        session, user, user_agent=request.headers.get("user-agent")
    )
    refresh_tokens.set_cookie(response, raw_refresh)

    return TokenResponse(
        access_token=_access_token_for(user),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate the refresh cookie and get a fresh access token",
)
async def refresh(
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """P1-7: обмен refresh-токена на новый access-токен.

    Токен читается только из httpOnly-куки: ни в теле, ни в query-параметре
    его не принимаем. Иначе он попадал бы в логи прокси и в историю
    браузера, и httpOnly перестал бы что-либо защищать.
    """
    raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token cookie is missing.",
        )

    try:
        raw_new, user = await refresh_tokens.rotate(
            session, raw, user_agent=request.headers.get("user-agent")
        )
    except refresh_tokens.RefreshError as exc:
        # Отзыв обязан уцелеть. get_session, увидев исключение, откатывает
        # транзакцию — без явного коммита массовый отзыв при обнаружении
        # кражи бесследно исчезал бы, и антифрод существовал бы только в
        # тексте ответа.
        await session.commit()

        # Ответ собираем сами, а не через HTTPException: заголовки
        # внедрённого Response попадают только в успешный ответ, и на
        # исключении Set-Cookie потерялся бы. Браузер продолжал бы носить
        # мёртвую куку и получать на каждый refresh один и тот же отказ.
        #
        # 403 при краже, 401 в остальных случаях. Разные коды не для
        # красоты: по 401 клиент молча уходит на логин, а 403 здесь значит
        # «все сессии отозваны» — об этом человеку нужно сказать.
        failure = JSONResponse(
            status_code=(
                status.HTTP_403_FORBIDDEN if exc.theft_detected else status.HTTP_401_UNAUTHORIZED
            ),
            content={"detail": exc.detail},
        )
        refresh_tokens.clear_cookie(failure)
        return failure

    refresh_tokens.set_cookie(response, raw_new)
    return TokenResponse(
        access_token=_access_token_for(user),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the refresh token and clear the cookie",
)
async def logout(
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """P1-7: выход.

    Идемпотентен и намеренно нетребователен: 204 и без куки, и с чужой, и с
    уже отозванной. Выход — это желаемое состояние «сессии нет», и оно
    достигнуто в каждом из этих случаев. Ошибка здесь означала бы оставить
    человека залогиненным из-за того, что он уже вышел.

    Access-токен не требуется: если refresh-кука утекла, отозвать её должно
    быть можно и без действующего access-токена.
    """
    raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if raw:
        token = await refresh_tokens.find_by_raw(session, raw)
        if token is not None:
            await refresh_tokens.revoke(session, token)

    refresh_tokens.clear_cookie(response)


@router.get(
    "/me",
    response_model=VolunteerProfile | StaffProfile | CoordinatorProfile,
    summary="Get current user profile (role-dependent)",
)
async def get_me(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user.role == UserRole.volunteer:
        vol = current_user.volunteer
        if vol is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Volunteer profile missing for user.",
            )
        return await _volunteer_profile(session, current_user, vol)

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
            organization=_organization_out(st.organization),
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
