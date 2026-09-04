"""
Pydantic v2 schemas for request / response serialization.
"""

from __future__ import annotations

import uuid
from datetime import datetime
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
    HypothesisStatus,
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
    is_over_14: bool

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
    stepik_cert_url: str | None = None


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
    stepik_cert_url: HttpUrl


class VolunteerProfileOut(BaseModel):
    """Returned from certificate endpoint (includes cert URL)."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    is_trained: bool
    is_over_14: bool
    stepik_cert_url: str | None


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
