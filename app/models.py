"""
SQLAlchemy ORM models.

Tables
------
- users          — base identity (email, hashed password, role)
- volunteers     — 1-to-1 extension for role='volunteer'
- organizations  — ООПТ / nature-reserve organizations
- staff          — 1-to-1 extension for role='staff', FK → organizations
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    volunteer = "volunteer"
    staff = "staff"


class OrgVerificationStatus(str, enum.Enum):
    """Result of external INN verification."""
    pending = "pending"
    verified = "verified"
    failed = "failed"
    manual_review = "manual_review"  # set when external API is unreachable


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        String(320), unique=True, nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", create_constraint=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Back-references (lazy="joined" keeps queries simple for the MVP)
    volunteer: Mapped[Volunteer | None] = relationship(
        back_populates="user", uselist=False, lazy="joined"
    )
    staff: Mapped[Staff | None] = relationship(
        back_populates="user", uselist=False, lazy="joined"
    )


# ---------------------------------------------------------------------------
# Volunteers
# ---------------------------------------------------------------------------

class Volunteer(Base):
    __tablename__ = "volunteers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    is_trained: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_over_14: Mapped[bool] = mapped_column(Boolean, nullable=False)

    user: Mapped[User] = relationship(back_populates="volunteer")


# ---------------------------------------------------------------------------
# Organizations (ООПТ)
# ---------------------------------------------------------------------------

class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    inn: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)
    cadastral_number: Mapped[str] = mapped_column(String(64), nullable=True)

    # Territory geometry stored as GeoJSON in JSONB.
    # Switch to `geoalchemy2.Geometry("POLYGON", srid=4326)` when PostGIS is
    # enabled — the rest of the code stays the same.
    territory_geom: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    verification_status: Mapped[OrgVerificationStatus] = mapped_column(
        SAEnum(
            OrgVerificationStatus,
            name="org_verification_status",
            create_constraint=True,
        ),
        default=OrgVerificationStatus.pending,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    staff_members: Mapped[list[Staff]] = relationship(
        back_populates="organization", lazy="selectin"
    )


# ---------------------------------------------------------------------------
# Staff (сотрудники ООПТ)
# ---------------------------------------------------------------------------

class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="staff")
    organization: Mapped[Organization] = relationship(back_populates="staff_members")
