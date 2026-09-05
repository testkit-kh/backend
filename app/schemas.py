"""
Pydantic v2 schemas for request / response serialization.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.cleanup_cost import AccessType, TrashCategory, TrashFraction
from app.models import (
    CertificateStatus,
    ConsentStatus,
    EducationLevel,
    EventStatus,
    HypothesisStatus,
    NotificationKind,
    OrgVerificationStatus,
    ParcelStatus,
    UserRole,
)

# ═══════════════════════════════════════════════════════════════════════════
# Auth — requests
# ═══════════════════════════════════════════════════════════════════════════


class VolunteerRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=256)

    #: Дата рождения вместо флага «мне есть 14»: 14–17 требуют согласия
    #: законного представителя, а флаг этого различия не несёт. Опциональна
    #: ради обратной совместимости со старым клиентом, который шлёт is_over_14.
    birth_date: date | None = None
    is_over_14: bool = True

    # Attribution — feeds the "conversion by channel" KPI. Optional: an
    # unknown channel is recorded as such rather than rejected.
    source: str | None = Field(
        default=None,
        max_length=64,
        description="Acquisition channel: school / social / referral / direct",
    )
    referred_by: uuid.UUID | None = Field(
        default=None,
        description="User id from the referral link, if the visitor came through one",
    )


class OrganizationRegisterRequest(BaseModel):
    # Organization fields
    org_name: str = Field(min_length=1, max_length=512)
    inn: str = Field(min_length=10, max_length=12, pattern=r"^\d{10,12}$")
    cadastral_number: str | None = Field(default=None, max_length=64)

    # First admin-staff user
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=256)


class CoordinatorRegisterRequest(BaseModel):
    """Регистрация координатора программы.

    Открытой быть не может: координатор видит все сертификаты и согласия.
    Код берётся из COORDINATOR_INVITE_CODE, не из таблицы инвайтов ООПТ.
    """

    invite_code: str = Field(min_length=8, max_length=128)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=256)


class StaffRegisterRequest(BaseModel):
    """Регистрация сотрудника по инвайту (P1-6).

    Организация не передаётся: она берётся из инвайта. Иначе приглашённый
    подставил бы чужую ООПТ, и код превратился бы из пропуска в одну
    организацию в пропуск в любую.
    """

    invite_code: str = Field(
        min_length=8,
        max_length=64,
        description="Одноразовый код, выданный сотрудником ООПТ",
    )
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=256)

    @field_validator("invite_code")
    @classmethod
    def _normalize_code(cls, value: str) -> str:
        """Код диктуют голосом и пересылают в мессенджере: регистр и
        обрамляющие пробелы к его смыслу отношения не имеют.
        """
        return value.strip().upper()


class LoginRequest(BaseModel):
    """Mirrors OAuth2PasswordRequestForm for JSON body fallback."""

    username: str  # email
    password: str


# ═══════════════════════════════════════════════════════════════════════════
# Auth — responses
# ═══════════════════════════════════════════════════════════════════════════


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    #: Сколько секунд жить access-токену. Клиент должен обновляться заранее,
    #: а не ловить первый 401 — иначе каждый цикл обновления стоит
    #: пользователю одного упавшего запроса.
    #: Сам refresh-токен в теле не возвращается никогда: только httpOnly-
    #: кукой, недоступной JavaScript.
    expires_in: int | None = None


# ═══════════════════════════════════════════════════════════════════════════
# User / profile responses
# ═══════════════════════════════════════════════════════════════════════════


class UserBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    created_at: datetime


class VolunteerProfile(UserBase):
    is_trained: bool
    is_over_14: bool
    birth_date: date | None = None
    consent_status: ConsentStatus = ConsentStatus.not_required
    certificate_status: CertificateStatus = CertificateStatus.none
    certificate_url: str | None = None
    #: Последнее поданное согласие. None — документ ещё не отправляли,
    #: даже если consent_status=awaiting (так бывает сразу после регистрации
    #: несовершеннолетнего). Без этого поля фронт путает «нужно» и «подано».
    latest_consent: ParentalConsentOut | None = None


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inn: str
    cadastral_number: str | None
    verification_status: OrgVerificationStatus
    created_at: datetime
    territory_source: str | None = None
    territory_osm_id: str | None = None
    has_territory: bool = False


class StaffProfile(UserBase):
    organization: OrganizationOut


class CoordinatorProfile(UserBase):
    """Координатор программы со стороны Фонда.

    Кроме базовых полей пользователя добавить нечего: координатор не
    привязан к организации и не проходит курс/согласие — это программная
    роль, а не территориальная и не волонтёрская.
    """


# Union-like response for /auth/me
UserProfileResponse = VolunteerProfile | StaffProfile | CoordinatorProfile


# ═══════════════════════════════════════════════════════════════════════════
# Organization profile — кабинет ООПТ + верификация координатором
# ═══════════════════════════════════════════════════════════════════════════


class StaffMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    email: str


class OrganizationProfileOut(BaseModel):
    """Профиль организации для кабинета сотрудника ООПТ.

    Расширяет OrganizationOut списком сотрудников и счётчиками участков /
    площадок наблюдений — то, с чего сотрудник обычно начинает работу в
    кабинете, не переходя на другие вкладки.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inn: str
    cadastral_number: str | None
    verification_status: OrgVerificationStatus
    created_at: datetime
    contact_email: str | None = None
    contact_phone: str | None = None
    description: str | None = None
    territory_source: str | None = None
    territory_osm_id: str | None = None
    has_territory: bool = False

    staff_members: list[StaffMemberOut] = Field(default_factory=list)
    parcels_count: int = 0
    monitoring_sites_count: int = 0


class OrganizationUpdateRequest(BaseModel):
    """PATCH-семантика: применяются только переданные поля.

    Название и ИНН сюда не входят — они канонические, взяты из ЕГРЮЛ при
    регистрации, и правка руками означала бы разойтись с реестром.
    """

    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=4096)


class StaffInviteOut(BaseModel):
    """Ответ на выдачу инвайта.

    Код возвращается ровно один раз — в этом ответе. Ручки «покажи код ещё
    раз» нет намеренно: код и есть пропуск, и чем меньше мест, где его можно
    прочитать, тем лучше.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    organization_id: uuid.UUID
    expires_at: datetime
    created_at: datetime


class OrganizationListItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inn: str
    cadastral_number: str | None
    verification_status: OrgVerificationStatus
    created_at: datetime


class OrganizationVerifyRequest(BaseModel):
    approved: bool
    reason: str | None = Field(
        default=None,
        max_length=1024,
        description="Обязательна при отказе — организация должна понимать, что исправить",
    )

    @model_validator(mode="after")
    def _reason_required_on_reject(self) -> OrganizationVerifyRequest:
        if not self.approved and not (self.reason or "").strip():
            raise ValueError("reason is required when rejecting an organization")
        return self


# ═══════════════════════════════════════════════════════════════════════════
# Hypotheses
# ═══════════════════════════════════════════════════════════════════════════


class TrashDetails(BaseModel):
    """Характеристики мусора — их заполняет человек на месте.

    Всё опциональное: волонтёр на берегу может знать состав, но не объём, или
    наоборот. Требовать полноту здесь — значит потерять точку целиком.
    Оценка стоимости считается по тому, что заполнено; не хватает данных —
    поля сметы остаются пустыми, а не заполняются нулями.
    """

    trash_categories: list[TrashCategory] | None = Field(
        default=None, description="Что именно лежит на точке"
    )
    dominant_category: TrashCategory | None = Field(
        default=None, description="Преобладающий тип — по нему берётся плотность"
    )
    fraction: TrashFraction | None = Field(
        default=None, description="mega >1 м, macro 2.5 см–1 м, meso 0.5–2.5 см, micro <0.5 см"
    )
    access_type: AccessType | None = Field(
        default=None, description="Как добраться — главный множитель стоимости уборки"
    )
    estimated_area_m2: float | None = Field(default=None, gt=0, le=1_000_000)
    estimated_volume_m3: float | None = Field(default=None, gt=0, le=100_000)

    @field_validator("trash_categories")
    @classmethod
    def _no_duplicates(cls, value: list[TrashCategory] | None) -> list[TrashCategory] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("trash_categories must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _dominant_is_listed(self) -> TrashDetails:
        """Преобладающий тип обязан быть среди перечисленных — иначе смета
        посчитается по плотности того, чего на точке нет."""
        if (
            self.dominant_category is not None
            and self.trash_categories
            and self.dominant_category not in self.trash_categories
        ):
            raise ValueError("dominant_category must be one of trash_categories")
        return self


class CleanupEstimateOut(BaseModel):
    """Расчёт уборки. `assumptions` отдаётся наружу намеренно: ООПТ должен
    видеть, из каких коэффициентов получилась сумма."""

    volume_m3: float
    mass_kg: float
    handling_rub: float
    mobilisation_rub: float
    total_rub: float
    assumptions: dict[str, Any]


class HypothesisCreateRequest(BaseModel):
    """Создание гипотезы.

    Поддерживает два формата ввода геометрии:
    - Устаревший: lat + lon (обратная совместимость).
    - Новый: geometry (GeoJSON Feature или Geometry).
    Если передано geometry — lat/lon извлекаются из centroid'а
    на сервере; если только lat/lon — geometry не обязателен.
    """

    # ---- Геометрия (предпочтительный путь) ----
    geometry: GeoJSONGeometry | None = Field(
        default=None,
        description=("GeoJSON Geometry: Point или Polygon. При передаче lat/lon можно опустить."),
    )

    # ---- Обратная совместимость: lat/lon ----
    lat: float | None = Field(
        default=None,
        ge=-90.0,
        le=90.0,
        description="Latitude (WGS-84). Обязателен без geometry.",
    )
    lon: float | None = Field(
        default=None,
        ge=-180.0,
        le=180.0,
        description="Longitude (WGS-84). Обязателен без geometry.",
    )

    description: str = Field(
        min_length=1,
        max_length=4096,
    )
    photo_url: str | None = Field(
        default=None,
        max_length=2048,
    )
    trash: TrashDetails = Field(default_factory=TrashDetails)
    monitoring_site_id: uuid.UUID | None = Field(
        default=None,
        description=("Если точка — очередной замер на площадке многолетних наблюдений"),
    )

    # ---- P0-1: офлайн-идемпотентность ----
    # UUID, сгенерированный мобильным приложением на устройстве.
    # Пара (author_id, client_id) уникальна в БД — повторный
    # POST вернёт 200 и существующую запись, а не дубль.
    client_id: uuid.UUID | None = Field(
        default=None,
        description="Идемпотентный ключ от клиента",
    )
    created_at_client: datetime | None = Field(
        default=None,
        description=("Время создания на устройстве. Для определения offline-режима."),
    )

    @model_validator(mode="after")
    def _require_coordinates(self) -> HypothesisCreateRequest:
        """Нужен хотя бы один источник координат:
        либо geometry, либо пара lat+lon.
        """
        has_geom = self.geometry is not None
        has_latlon = self.lat is not None and self.lon is not None
        if not has_geom and not has_latlon:
            raise ValueError("Передайте geometry (GeoJSON) или оба поля lat + lon.")
        return self

    @field_validator("geometry")
    @classmethod
    def _validate_geometry(
        cls,
        value: GeoJSONGeometry | None,
    ) -> GeoJSONGeometry | None:
        """Строгая проверка GeoJSON: тип и структура координат."""
        if value is None:
            return None
        allowed = {"Point", "Polygon", "MultiPolygon"}
        if value.type not in allowed:
            raise ValueError(f"geometry.type должен быть одним из: {', '.join(sorted(allowed))}")
        coords = value.coordinates
        if value.type == "Point":
            if not isinstance(coords, list) or len(coords) < 2:
                raise ValueError("Point.coordinates: [lon, lat]")
            lon, lat = coords[0], coords[1]
            if not (-180 <= lon <= 180):
                raise ValueError(f"lon={lon} вне диапазона [-180, 180]")
            if not (-90 <= lat <= 90):
                raise ValueError(f"lat={lat} вне диапазона [-90, 90]")
        elif value.type == "Polygon":
            # Минимум один линейный кольцевой массив
            if not isinstance(coords, list) or len(coords) < 1:
                raise ValueError("Polygon.coordinates: минимум одно кольцо")
            ring = coords[0]
            if not isinstance(ring, list) or len(ring) < 4:
                raise ValueError("Polygon: кольцо должно содержать минимум 4 точки")
            if ring[0] != ring[-1]:
                raise ValueError("Polygon: кольцо должно быть замкнутым (first == last)")
        return value


class HypothesisValidateRequest(BaseModel):
    status: HypothesisStatus = Field(
        description="Новый статус гипотезы",
    )
    reason: str | None = Field(
        default=None,
        max_length=1024,
        description=(
            "Причина отказа. Обязательна при rejected — волонтёр видит её в ленте «Мои точки»"
        ),
    )

    @field_validator("status")
    @classmethod
    def _status_is_a_verdict(
        cls,
        value: HypothesisStatus,
    ) -> HypothesisStatus:
        """pending — начальное состояние, а не вердикт,
        который может установить модератор.

        cleaned — тоже не вердикт: он ставится закрытием
        мероприятия, а не рукой модератора.
        """
        allowed = {
            HypothesisStatus.approved,
            HypothesisStatus.rejected,
            HypothesisStatus.drone_requested,
        }
        if value not in allowed:
            raise ValueError(f"Status must be one of: {', '.join(s.value for s in allowed)}")
        return value

    @model_validator(mode="after")
    def _reason_required_on_reject(
        self,
    ) -> HypothesisValidateRequest:
        """Отказ без причины волонтёр прочитать не может:
        он видит «нет» и не знает, что исправить.
        """
        if self.status == HypothesisStatus.rejected and not (self.reason or "").strip():
            raise ValueError("reason is required when rejecting a hypothesis")
        return self


class HypothesisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID | None
    organization_id: uuid.UUID | None
    lat: float
    lon: float
    description: str
    photo_url: str | None
    status: HypothesisStatus
    reject_reason: str | None = None

    # P0-1: офлайн-поля
    client_id: uuid.UUID | None = None
    created_at_client: datetime | None = None

    trash_categories: list[str] | None = None
    dominant_category: TrashCategory | None = None
    fraction: TrashFraction | None = None
    access_type: AccessType | None = None
    estimated_area_m2: float | None = None
    estimated_volume_m3: float | None = None
    computed_volume_m3: float | None = None
    computed_mass_kg: float | None = None
    cleanup_cost_rub: float | None = None
    cost_assumptions: dict[str, Any] | None = None
    monitoring_site_id: uuid.UUID | None = None

    created_at: datetime
    updated_at: datetime


class HypothesisValidateResponse(BaseModel):
    hypothesis: HypothesisOut
    event_id: uuid.UUID | None = None


# ═══════════════════════════════════════════════════════════════════════════
# P1-5 — лента «Мои точки»
# ═══════════════════════════════════════════════════════════════════════════


class MyHypothesisOut(BaseModel):
    """Одна точка в личной ленте волонтёра.

    Урезанная проекция HypothesisOut: смета и коэффициенты — рабочие данные
    ООПТ, автору точки они не нужны. Зато нужны причина отказа и судьба
    точки: попала ли она в мероприятие и когда его уберут.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: HypothesisStatus
    description: str
    #: Заполнена только при status == rejected.
    reject_reason: str | None = None

    lat: float
    lon: float
    photo_url: str | None = None
    organization_id: uuid.UUID | None = None

    # Мероприятие, в которое превратилась точка. Для волонтёра это ответ на
    # «а что с ней дальше»: без него одобренная точка выглядит так же, как
    # забытая.
    event_id: uuid.UUID | None = None
    event_status: EventStatus | None = None
    event_scheduled_at: datetime | None = None

    created_at: datetime
    updated_at: datetime


class MyHypothesesListOut(BaseModel):
    """Страница ленты.

    Курсор, а не offset: между страницами волонтёру могут отказать в точке
    или закрыть мероприятие, и сдвиг «на 20» пропустил бы строку или
    показал её дважды. `total` считается отдельно — длина items про размер
    ленты ничего не говорит.
    """

    items: list[MyHypothesisOut]
    total: int
    limit: int
    next_cursor: str | None = None


# ═══════════════════════════════════════════════════════════════════════════
# P1-4 — мероприятия по уборке
# ═══════════════════════════════════════════════════════════════════════════


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hypothesis_id: uuid.UUID
    organization_id: uuid.UUID
    title: str
    description: str | None = None
    place: str | None = None
    scheduled_at: datetime | None = None
    status: EventStatus

    #: Сколько человек записалось. Не путать с actual_participants — тем,
    #: сколько пришло.
    participants_count: int = 0
    #: Записан ли текущий пользователь. Всегда False в ответах сотруднику.
    is_joined: bool = False

    # ---- Итоги, заполняются при закрытии ----
    completed_at: datetime | None = None
    actual_participants: int | None = None
    waste_volume_m3: float | None = None
    waste_mass_kg: float | None = None
    result_notes: str | None = None

    created_at: datetime
    updated_at: datetime

    # ---- Приёмка «до/после» ----
    photo_before_urls: list[str] | None = None
    photo_after_urls: list[str] | None = None
    before_after_accepted_at: datetime | None = None


class EventListOut(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int


class EventUpdateRequest(BaseModel):
    """PATCH-семантика: применяются только переданные поля.

    Не переданное поле и переданный null — разные вещи (не трогать против
    «стереть»), поэтому обработчик смотрит на model_fields_set, а не на
    None.
    """

    scheduled_at: datetime | None = None
    place: str | None = Field(default=None, max_length=512)
    description: str | None = Field(default=None, max_length=4096)

    @field_validator("scheduled_at")
    @classmethod
    def _tz_aware(cls, value: datetime | None) -> datetime | None:
        """Наивную дату считаем UTC: колонка timestamptz, и asyncpg
        не примет datetime без зоны.
        """
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @model_validator(mode="after")
    def _not_empty(self) -> EventUpdateRequest:
        if not self.model_fields_set:
            raise ValueError(
                "Нужно передать хотя бы одно из полей: scheduled_at, place, description"
            )
        return self


class EventCompleteRequest(BaseModel):
    """Итоги уборки.

    Объём или масса — хотя бы одно: без них закрытие мероприятия не даёт
    проекту ничего, а именно эти числа идут в отчётность по вывезенному
    мусору.
    """

    actual_participants: int = Field(
        ge=0,
        le=10_000,
        description="Сколько человек реально пришло",
    )
    waste_volume_m3: float | None = Field(
        default=None,
        ge=0,
        le=100_000,
        description="Объём собранного мусора, м³",
    )
    waste_mass_kg: float | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
        description="Масса собранного мусора, кг",
    )
    result_notes: str | None = Field(default=None, max_length=4096)
    completed_at: datetime | None = Field(
        default=None,
        description="Фактическое время окончания; по умолчанию — сейчас",
    )
    #: Кто из записавшихся пришёл. Не передан — явку не размечаем:
    #: пустой список и «не отмечали» это разные ситуации.
    attended_user_ids: list[uuid.UUID] | None = Field(
        default=None,
        description="Участники, отмеченные как пришедшие",
    )

    @field_validator("completed_at")
    @classmethod
    def _tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @model_validator(mode="after")
    def _volume_or_mass(self) -> EventCompleteRequest:
        if self.waste_volume_m3 is None and self.waste_mass_kg is None:
            raise ValueError(
                "Нужен объём или масса собранного мусора: waste_volume_m3 или waste_mass_kg"
            )
        return self


class EventCompleteResponse(BaseModel):
    """Ответ закрытия мероприятия.

    Статус гипотезы возвращается явно: смена точки на cleaned — побочный
    эффект этого вызова, и клиент должен видеть, что он произошёл, не
    перезапрашивая точку.
    """

    event: EventOut
    hypothesis_id: uuid.UUID
    hypothesis_status: HypothesisStatus
    attendance_marked: int = 0


class EventBeforeAfterRequest(BaseModel):
    """Приёмка фотографий «до» и «после» уборки.

    Хотя бы одно фото «после» обязательно: без него принимать нечего.
    «До» можно не слать — тогда берётся photo_url гипотезы, ради которой
    мероприятие создали.
    """

    photo_before_urls: list[str] = Field(default_factory=list, max_length=20)
    photo_after_urls: list[str] = Field(min_length=1, max_length=20)

    @field_validator("photo_before_urls", "photo_after_urls")
    @classmethod
    def _urls(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        for item in cleaned:
            if len(item) > 2048:
                raise ValueError("photo URL must be at most 2048 characters")
        return cleaned

    @model_validator(mode="after")
    def _after_required(self) -> EventBeforeAfterRequest:
        if not self.photo_after_urls:
            raise ValueError("at least one after photo is required")
        return self


class EventBeforeAfterOut(BaseModel):
    event: EventOut
    #: True, если приёмка уже была: повтор не переписывает фото и не
    #: эмитит второе событие.
    already_accepted: bool = False


class EventJoinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: uuid.UUID
    user_id: uuid.UUID | None
    joined_at: datetime
    attended: bool
    #: True, если запись уже существовала. Вместе с кодом 200 (а не 201)
    #: отличает повтор от новой записи.
    already_joined: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# Volunteers — certificate
# ═══════════════════════════════════════════════════════════════════════════


class CertificateRequest(BaseModel):
    certificate_url: HttpUrl


class VolunteerProfileOut(BaseModel):
    """Returned from certificate endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    is_trained: bool
    is_over_14: bool
    certificate_url: str | None
    certificate_status: CertificateStatus
    certificate_submitted_at: datetime | None = None
    certificate_reviewed_at: datetime | None = None
    certificate_reject_reason: str | None = None


class CertificateReviewRequest(BaseModel):
    approved: bool
    reason: str | None = Field(
        default=None,
        max_length=1024,
        description="Обязательна при отказе — волонтёр должен понимать, что исправить",
    )

    @model_validator(mode="after")
    def _reason_required_on_reject(self) -> CertificateReviewRequest:
        if not self.approved and not (self.reason or "").strip():
            raise ValueError("reason is required when rejecting a certificate")
        return self


class PendingCertificateOut(BaseModel):
    volunteer_id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    email: str
    certificate_url: str | None
    certificate_submitted_at: datetime | None
    course_redirect_at: datetime | None


class CourseRedirectOut(BaseModel):
    """Куда идти дальше. JSON, а не HTTP-редирект: ручка требует Bearer-токен,
    а обычная навигация браузера (клик по `<a href>`) заголовков не шлёт —
    поэтому фронт делает авторизованный запрос сюда и сам переходит по url."""

    url: str


class CourseStatusOut(BaseModel):
    """Один экран «где я на пути обучения» — чтобы фронт не собирал его из
    трёх разных ручек."""

    course_url: str
    certificate_status: CertificateStatus
    certificate_url: str | None
    certificate_submitted_at: datetime | None
    certificate_reviewed_at: datetime | None
    certificate_reject_reason: str | None
    course_redirect_at: datetime | None
    map_access_granted_at: datetime | None
    has_map_access: bool


# ═══════════════════════════════════════════════════════════════════════════
# Notifications
# ═══════════════════════════════════════════════════════════════════════════


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: NotificationKind
    title: str
    body: str | None
    action_url: str | None
    payload: dict[str, Any]
    read_at: datetime | None
    clicked_at: datetime | None
    created_at: datetime


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    unread_count: int


class ReminderDispatchOut(BaseModel):
    """Итог рассылки. `due` при dry_run — сколько бы ушло."""

    sent: int
    due: int
    preview: list[str]


# ═══════════════════════════════════════════════════════════════════════════
# GeoJSON — map layers
# ═══════════════════════════════════════════════════════════════════════════


class GeoJSONGeometry(BaseModel):
    type: str
    coordinates: Any


class GeoJSONProperties(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    # Flexible key-value payload — each feature fills this differently.
    id: uuid.UUID | None = None
    name: str | None = None
    status: str | None = None
    description: str | None = None
    layer: str | None = None


class GeoJSONFeature(BaseModel):
    type: str = "Feature"
    geometry: GeoJSONGeometry
    properties: GeoJSONProperties


class GeoJSONFeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    features: list[GeoJSONFeature]


# ═══════════════════════════════════════════════════════════════════════════
# Monitoring sites — площадки многолетних наблюдений
# ═══════════════════════════════════════════════════════════════════════════


class MonitoringSiteCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    code: str = Field(
        min_length=2,
        max_length=32,
        pattern=r"^[A-Z0-9\-]+$",
        description="Внутренний код площадки в методике Фонда, например KRO-01",
    )
    established_at: datetime
    protocol: str | None = Field(default=None, max_length=8192)
    area_m2: float | None = Field(default=None, gt=0)
    shoreline_length_m: float | None = Field(default=None, gt=0)
    # Полигон площадки в GeoJSON. Необязателен: площадку можно завести заранее,
    # а границы уточнить после первого выезда.
    geometry: GeoJSONGeometry | None = None


class MonitoringSiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    code: str
    area_m2: float | None
    shoreline_length_m: float | None
    established_at: datetime
    protocol: str | None
    is_active: bool
    created_at: datetime
    surveys_count: int = 0
    last_surveyed_at: datetime | None = None


class SiteSurveyCreateRequest(BaseModel):
    surveyed_at: datetime
    trash: TrashDetails = Field(default_factory=TrashDetails)
    item_count: int | None = Field(default=None, ge=0)
    was_cleaned: bool = False
    photo_urls: list[str] | None = None
    notes: str | None = Field(default=None, max_length=8192)


class SiteSurveyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    site_id: uuid.UUID
    author_id: uuid.UUID | None
    surveyed_at: datetime
    trash_categories: list[str] | None
    dominant_category: TrashCategory | None
    fraction: TrashFraction | None
    item_count: int | None
    estimated_area_m2: float | None
    estimated_volume_m3: float | None
    computed_volume_m3: float | None
    computed_mass_kg: float | None
    was_cleaned: bool
    photo_urls: list[str] | None
    notes: str | None
    created_at: datetime


class AccumulationInterval(BaseModel):
    """Накопление между двумя соседними замерами.

    Ради этого числа площадки и закладываются: разовый замер говорит «здесь
    столько-то мусора», а пара замеров — «столько-то приносит в месяц», и вот
    это уже позволяет планировать выезды и считать бюджет.
    """

    from_surveyed_at: datetime
    to_surveyed_at: datetime
    days: float
    # None, если предыдущий замер не сопровождался уборкой: тогда разница
    # объёмов — это не скорость накопления, а накопленный итог.
    volume_delta_m3: float | None
    mass_delta_kg: float | None
    kg_per_day: float | None
    kg_per_100m_per_day: float | None
    baseline_cleaned: bool


class SiteAccumulationOut(BaseModel):
    site_id: uuid.UUID
    code: str
    shoreline_length_m: float | None
    intervals: list[AccumulationInterval]
    mean_kg_per_day: float | None


# ═══════════════════════════════════════════════════════════════════════════
# Registry — автозаполнение по ИНН
# ═══════════════════════════════════════════════════════════════════════════


class CompanyInfoOut(BaseModel):
    """Сведения из ЕГРЮЛ. `source` наружу отдаётся намеренно: фронт должен
    показывать, откуда данные, а на защите — что источник первичный."""

    inn: str
    name: str
    short_name: str | None = None
    ogrn: str | None = None
    kpp: str | None = None
    address: str | None = None
    region: str | None = None
    management: str | None = None
    registered_at: str | None = None
    entity_type: str | None = None
    is_active: bool = True
    source: str


# ═══════════════════════════════════════════════════════════════════════════
# Parental consent
# ═══════════════════════════════════════════════════════════════════════════


class ParentalConsentCreateRequest(BaseModel):
    representative_name: str = Field(min_length=1, max_length=256)
    representative_phone: str = Field(min_length=5, max_length=32, pattern=r"^[\d\s\-\+\(\)]+$")
    representative_email: EmailStr
    relation: str | None = Field(default=None, max_length=64, description="мать / отец / опекун")
    scan_url: HttpUrl | None = Field(default=None, description="Скан подписанного согласия")


class ParentalConsentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    volunteer_id: uuid.UUID
    representative_name: str
    representative_phone: str
    representative_email: str
    relation: str | None
    scan_url: str | None
    status: ConsentStatus
    reject_reason: str | None
    submitted_at: datetime
    reviewed_at: datetime | None


class ConsentReviewRequest(BaseModel):
    approved: bool
    reason: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _reason_required_on_reject(self) -> ConsentReviewRequest:
        if not self.approved and not (self.reason or "").strip():
            raise ValueError("reason is required when rejecting a consent")
        return self


# ═══════════════════════════════════════════════════════════════════════════
# Cadastral parcels
# ═══════════════════════════════════════════════════════════════════════════


class CadastralParcelCreateRequest(BaseModel):
    cadastral_number: str = Field(
        min_length=10,
        max_length=64,
        description="Формат 41:01:0000000:1",
    )


class CadastralParcelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    cadastral_number: str
    area_ha: float | None
    status: ParcelStatus
    source: str | None
    resolve_error: str | None
    resolved_at: datetime | None
    created_at: datetime


class ParcelGeometryRequest(BaseModel):
    """Ручной ввод границ участка.

    Не запасной путь, а равноправный: ФГИС ЕГРН недоступен значительную часть
    времени, и ждать от него границы для всех участков нельзя.
    """

    geometry: GeoJSONGeometry

    @field_validator("geometry")
    @classmethod
    def _polygonal(cls, value: GeoJSONGeometry) -> GeoJSONGeometry:
        if value.type not in ("Polygon", "MultiPolygon"):
            raise ValueError("geometry must be a Polygon or MultiPolygon")
        return value


# ═══════════════════════════════════════════════════════════════════════════
# Analytics
# ═══════════════════════════════════════════════════════════════════════════


class DashboardEmbedOut(BaseModel):
    """Подписанная ссылка на дашборд Metabase.

    `expires_in` отдаётся, чтобы фронт перезапрашивал ссылку до истечения
    срока, а не показывал пользователю протухший iframe.
    """

    slug: str
    title: str
    url: str
    expires_in: int
    scoped_to_organization: bool


class AnalyticsSummaryOut(BaseModel):
    """Числа для плашек. Смысл зависит от роли: волонтёр видит свои точки,
    сотрудник — точки своей ООПТ, координатор — программу целиком."""

    pending: int
    approved: int
    rejected: int
    drone_requested: int
    confirmed_volume_m3: float
    confirmed_cleanup_cost_rub: float
    certificate_status: CertificateStatus


# ═══════════════════════════════════════════════════════════════════════════
# Education, territory, uploads
# ═══════════════════════════════════════════════════════════════════════════


class EducationRequest(BaseModel):
    level: EducationLevel
    institution_name: str | None = Field(default=None, max_length=512)
    institution_inn: str | None = Field(default=None, max_length=12)
    grade: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=128)

    @field_validator("institution_name", "grade", "city")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("institution_inn")
    @classmethod
    def _digits_inn(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return "".join(ch for ch in value if ch.isdigit())


class EducationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    level: EducationLevel
    institution_name: str | None
    institution_inn: str | None
    registry_name: str | None = None
    grade: str | None
    city: str | None
    updated_at: datetime


class TerritoryUpdateRequest(BaseModel):
    """Границы ООПТ без кадастра — полигон из OSM, который фронт уже держит."""

    source: str = Field(min_length=1, max_length=32)
    osm_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=512)
    geometry: GeoJSONGeometry

    @field_validator("source")
    @classmethod
    def _known_source(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"osm", "manual"}:
            raise ValueError("source must be 'osm' or 'manual'")
        return normalized

    @field_validator("geometry")
    @classmethod
    def _polygonal(cls, value: GeoJSONGeometry) -> GeoJSONGeometry:
        if value.type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("Territory geometry must be a Polygon or MultiPolygon.")
        return value


class TerritoryOut(BaseModel):
    organization_id: uuid.UUID
    source: str
    osm_id: str | None
    name: str | None
    has_territory: bool


class UploadPresignRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=3, max_length=128)
    purpose: str = Field(default="hypothesis_photo", max_length=64)


class UploadPresignOut(BaseModel):
    method: str = "PUT"
    upload_url: str
    public_url: str
    headers: dict[str, str]
    expires_in: int
    key: str


VolunteerProfile.model_rebuild()
