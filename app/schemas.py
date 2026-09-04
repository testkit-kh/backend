"""
Pydantic v2 schemas for request / response serialization.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import OrgVerificationStatus, UserRole


# ---------------------------------------------------------------------------
# Auth — requests
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Auth — responses
# ---------------------------------------------------------------------------

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# User / profile responses
# ---------------------------------------------------------------------------

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
