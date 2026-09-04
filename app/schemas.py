"""
Pydantic v2 schemas for request / response serialization.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl

from app.models import (
    EventStatus,
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

class HypothesisCreateRequest(BaseModel):
    lat: float = Field(ge=-90.0, le=90.0, description="Latitude (WGS-84)")
    lon: float = Field(ge=-180.0, le=180.0, description="Longitude (WGS-84)")
    description: str = Field(min_length=1, max_length=4096)
    photo_url: str | None = Field(default=None, max_length=2048)


class HypothesisValidateRequest(BaseModel):
    status: HypothesisStatus = Field(
        description="New status for the hypothesis"
    )

    def model_post_init(self, __context: Any) -> None:
        allowed = {
            HypothesisStatus.approved,
            HypothesisStatus.rejected,
            HypothesisStatus.drone_requested,
        }
        if self.status not in allowed:
            raise ValueError(
                f"Status must be one of: {', '.join(s.value for s in allowed)}"
            )


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
