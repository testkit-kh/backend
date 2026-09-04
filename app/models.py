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

from geoalchemy2 import Geography, Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.cleanup_cost import AccessType, TrashCategory, TrashFraction
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
    cadastral_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

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

    # ---- Характеристики мусора (заполняет автор гипотезы) ----------------
    # Проект «Чистый берег» изучает не только факт загрязнения, но и состав,
    # фракцию и объём — без этих полей нельзя ни спрогнозировать затраты на
    # уборку, ни сравнить накопление между замерами на одной площадке.
    trash_categories: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(32)), nullable=True
    )
    dominant_category: Mapped[TrashCategory | None] = mapped_column(
        SAEnum(TrashCategory, name="trash_category"), nullable=True
    )
    fraction: Mapped[TrashFraction | None] = mapped_column(
        SAEnum(TrashFraction, name="trash_fraction"), nullable=True
    )
    access_type: Mapped[AccessType | None] = mapped_column(
        SAEnum(AccessType, name="access_type"), nullable=True
    )

    # Человек указывает либо объём, либо площадь пятна — что проще оценить
    # на месте. Второе пересчитывается через среднюю толщину слоя.
    estimated_area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_volume_m3: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Производные величины. Хранятся, а не считаются на лету: коэффициенты
    # со временем поменяются, а смета, показанная ООПТ, должна остаться той,
    # по которой принимали решение.
    computed_volume_m3: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_mass_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    cleanup_cost_rub: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_assumptions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Гипотеза может быть очередным замером на площадке многолетних наблюдений.
    monitoring_site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("monitoring_sites.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

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


# ---------------------------------------------------------------------------
# Analytics events — the single source for every KPI
# ---------------------------------------------------------------------------

class AnalyticsEvent(Base):
    """Append-only event log.

    Deliberately schema-light: `event_type` is a plain string (not a DB enum)
    so that adding a new event never requires a migration, and `payload` is
    JSONB so each event type carries its own fields. The KPI views in the
    `kpi` schema are the typed layer on top.

    Not to be confused with `Event` above — that one is a cleanup event in the
    field. This one is telemetry.
    """

    __tablename__ = "analytics_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Geography, not Geometry: KPI queries measure real distances in metres
    # (e.g. "points within the 20 km coastal buffer").
    geo = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_analytics_events_type_created", "event_type", "created_at"),
        Index("ix_analytics_events_user_created", "user_id", "created_at"),
        Index("ix_analytics_events_payload", "payload", postgresql_using="gin"),
        Index("ix_analytics_events_geo", "geo", postgresql_using="gist"),
    )


# ---------------------------------------------------------------------------
# Monitoring sites — площадки многолетних наблюдений
# ---------------------------------------------------------------------------

class MonitoringSite(Base):
    """Постоянная площадка, на которой замеры повторяются год за годом.

    Отличие от гипотезы: гипотеза — разовое «здесь мусор», площадка — участок
    с фиксированными границами, где по одной методике меряют, сколько мусора
    накопилось с прошлого раза. Именно из разницы между замерами получается
    скорость накопления — то, ради чего проект и закладывает такие площадки.
    """

    __tablename__ = "monitoring_sites"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # Внутренний код площадки в методике Фонда (например, KRO-01).
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    # Границы площадки. Полигон, а не точка: площадь нужна, чтобы приводить
    # замеры к единице площади и сравнивать площадки между собой.
    geom = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )
    area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Длина берегового отрезка, м — стандартная нормировка для пляжного учёта.
    shoreline_length_m: Mapped[float | None] = mapped_column(Float, nullable=True)

    established_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    protocol: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    organization: Mapped[Organization] = relationship(lazy="joined")
    surveys: Mapped[list[SiteSurvey]] = relationship(
        back_populates="site", order_by="SiteSurvey.surveyed_at"
    )

    __table_args__ = (
        Index("idx_monitoring_sites_geom", "geom", postgresql_using="gist"),
    )


class SiteSurvey(Base):
    """Один замер на площадке.

    Состав полей намеренно совпадает с полями гипотезы: и там, и там мусор
    описывает человек по одной методике, иначе замеры несопоставимы.
    """

    __tablename__ = "site_surveys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitoring_sites.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    surveyed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    trash_categories: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(32)), nullable=True
    )
    dominant_category: Mapped[TrashCategory | None] = mapped_column(
        SAEnum(TrashCategory, name="trash_category"), nullable=True
    )
    fraction: Mapped[TrashFraction | None] = mapped_column(
        SAEnum(TrashFraction, name="trash_fraction"), nullable=True
    )
    # Число собранных предметов — в методике учёта пляжного мусора это
    # основная величина, объём вторичен.
    item_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    estimated_area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_volume_m3: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_volume_m3: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_mass_kg: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Убрали ли мусор после замера. Если да, следующий замер меряет накопление
    # с нуля; если нет — накопленное с момента закладки площадки.
    was_cleaned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    photo_urls: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(2048)), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    site: Mapped[MonitoringSite] = relationship(back_populates="surveys")

    __table_args__ = (
        Index("ix_site_surveys_site_surveyed", "site_id", "surveyed_at"),
    )
