"""
Pydantic v2 schemas for request / response serialization.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
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
    ParcelStatus,
    HypothesisStatus,
    NotificationKind,
    OrgVerificationStatus,
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


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inn: str
    cadastral_number: str | None
    verification_status: OrgVerificationStatus
    created_at: datetime


class StaffProfile(UserBase):
    organization: OrganizationOut


# Union-like response for /auth/me
UserProfileResponse = VolunteerProfile | StaffProfile


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
    def _no_duplicates(
        cls, value: list[TrashCategory] | None
    ) -> list[TrashCategory] | None:
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
    lat: float = Field(ge=-90.0, le=90.0, description="Latitude (WGS-84)")
    lon: float = Field(ge=-180.0, le=180.0, description="Longitude (WGS-84)")
    description: str = Field(min_length=1, max_length=4096)
    photo_url: str | None = Field(default=None, max_length=2048)
    trash: TrashDetails = Field(default_factory=TrashDetails)
    monitoring_site_id: uuid.UUID | None = Field(
        default=None,
        description="Если точка — очередной замер на площадке многолетних наблюдений",
    )


class HypothesisValidateRequest(BaseModel):
    status: HypothesisStatus = Field(
        description="New status for the hypothesis"
    )

    @field_validator("status")
    @classmethod
    def _status_is_a_verdict(cls, value: HypothesisStatus) -> HypothesisStatus:
        """`pending` is the initial state, not a verdict a moderator can set.

        This must be a validator, not model_post_init: an error raised there
        escapes as a 500 instead of a 422.
        """
        allowed = {
            HypothesisStatus.approved,
            HypothesisStatus.rejected,
            HypothesisStatus.drone_requested,
        }
        if value not in allowed:
            raise ValueError(
                f"Status must be one of: {', '.join(s.value for s in allowed)}"
            )
        return value


class HypothesisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID
    organization_id: uuid.UUID | None
    lat: float
    lon: float
    description: str
    photo_url: str | None
    status: HypothesisStatus

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
    representative_phone: str = Field(
        min_length=5, max_length=32, pattern=r"^[\d\s\-\+\(\)]+$"
    )
    representative_email: EmailStr
    relation: str | None = Field(
        default=None, max_length=64, description="мать / отец / опекун"
    )
    scan_url: HttpUrl | None = Field(
        default=None, description="Скан подписанного согласия"
    )


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
