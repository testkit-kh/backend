"""
SQLAlchemy ORM models.

Tables
------
- users          — base identity (email, hashed password, role)
- volunteers     — 1-to-1 extension for role='volunteer'
- organizations  — ООПТ / nature-reserve organizations
- staff          — 1-to-1 extension for role='staff', FK → organizations
- hypotheses     — volunteer-submitted ecological observations
- events         — field events spawned from approved hypotheses
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    Uuid,
)
from sqlalchemy import (
    Enum as SAEnum,
)
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
    manual_review = "manual_review"


class HypothesisStatus(str, enum.Enum):
    """Lifecycle of a volunteer-submitted hypothesis."""
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    drone_requested = "drone_requested"


class EventStatus(str, enum.Enum):
    """Lifecycle of a field event."""
    planned = "planned"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


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
        default=lambda: datetime.now(UTC),
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
    stepik_cert_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True, default=None
    )

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

    # Native PostGIS polygon — SRID 4326 (WGS-84, lon/lat).
    # Requires CREATE EXTENSION postgis; in the database.
    territory_geom = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=True),
        nullable=True,
    )

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
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    staff_members: Mapped[list[Staff]] = relationship(
        back_populates="organization", lazy="selectin"
    )
    hypotheses: Mapped[list[Hypothesis]] = relationship(
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


# ---------------------------------------------------------------------------
# Hypotheses (экологические наблюдения волонтёров)
# ---------------------------------------------------------------------------

class Hypothesis(Base):
    __tablename__ = "hypotheses"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)

    # Native PostGIS point built from (lon, lat) at insertion time.
    location = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True),
        nullable=True,
    )

    description: Mapped[str] = mapped_column(Text, nullable=False)
    photo_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    status: Mapped[HypothesisStatus] = mapped_column(
        SAEnum(
            HypothesisStatus,
            name="hypothesis_status",
            create_constraint=True,
        ),
        default=HypothesisStatus.pending,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    author: Mapped[User] = relationship(foreign_keys=[author_id], lazy="joined")
    organization: Mapped[Organization | None] = relationship(
        back_populates="hypotheses", lazy="joined"
    )
    event: Mapped[Event | None] = relationship(
        back_populates="hypothesis", uselist=False, lazy="joined"
    )


# ---------------------------------------------------------------------------
# Events (мероприятия, создаются при approved-гипотезе)
# ---------------------------------------------------------------------------

class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hypotheses.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String(512), nullable=False
    )
    status: Mapped[EventStatus] = mapped_column(
        SAEnum(EventStatus, name="event_status", create_constraint=True),
        default=EventStatus.planned,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    hypothesis: Mapped[Hypothesis] = relationship(back_populates="event")
