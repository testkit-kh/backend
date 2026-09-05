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
from datetime import UTC, date, datetime

from geoalchemy2 import Geography, Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)

from app.cleanup_cost import AccessType, TrashCategory, TrashFraction
from app.database import Base

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    volunteer = "volunteer"
    staff = "staff"
    #: Координатор программы со стороны Фонда. Проверяет сертификаты по всем
    #: территориям — это программная роль, а не территориальная: инспектор
    #: Кроноцкого не должен решать, обучен ли волонтёр из Дагестана.
    coordinator = "coordinator"


class OrgVerificationStatus(str, enum.Enum):
    """Result of external INN verification."""
    pending = "pending"
    verified = "verified"
    failed = "failed"
    manual_review = "manual_review"


class CertificateStatus(str, enum.Enum):
    """Состояние сертификата волонтёра о прохождении курса."""

    #: Курс даже не начат — сертификат не присылали.
    none = "none"
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ConsentStatus(str, enum.Enum):
    """Согласие законного представителя на участие несовершеннолетнего."""

    #: 18+ — согласие не требуется.
    not_required = "not_required"
    awaiting = "awaiting"
    approved = "approved"
    rejected = "rejected"


class ParcelStatus(str, enum.Enum):
    """Состояние резолвинга кадастрового участка в геометрию."""

    pending = "pending"
    resolved = "resolved"
    #: Росреестр не отдал границы — участок остаётся в списке, границы
    #: вводятся вручную. Терять номер из-за недоступности ФГИС нельзя.
    failed = "failed"


class NotificationKind(str, enum.Enum):
    """Типы уведомлений. Строкой в БД не делаем: список закрытый и короткий,
    а опечатка в типе сломала бы и фильтрацию, и KPI по напоминаниям."""

    consent_required = "consent_required"
    consent_approved = "consent_approved"
    consent_rejected = "consent_rejected"
    course_not_started = "course_not_started"
    course_not_finished = "course_not_finished"
    certificate_approved = "certificate_approved"
    certificate_rejected = "certificate_rejected"
    point_validated = "point_validated"
    cleanup_event_invite = "cleanup_event_invite"


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
    # foreign_keys обязателен: volunteers ссылается на users дважды —
    # владелец (user_id) и проверяющий сертификат (certificate_reviewer_id).
    volunteer: Mapped[Volunteer | None] = relationship(
        back_populates="user",
        uselist=False,
        lazy="joined",
        foreign_keys="Volunteer.user_id",
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
    #: Производное от certificate_status == approved. Отдельным полем — чтобы
    #: горячая проверка «пускать ли на карту» не читала enum на каждом запросе.
    is_trained: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_over_14: Mapped[bool] = mapped_column(Boolean, nullable=False)

    #: Точная дата рождения. `is_over_14` из неё выводится, но остаётся:
    #: нужна дата, а не флаг, потому что 14–17 требуют согласия родителя,
    #: и в день восемнадцатилетия требование должно сниматься само.
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    consent_status: Mapped[ConsentStatus] = mapped_column(
        SAEnum(ConsentStatus, name="consent_status"),
        default=ConsentStatus.not_required,
        nullable=False,
    )

    # ---- Обучение: «Школа Защитников Природы» (iSpring) --------------------
    # Названия нейтральные (course_/certificate_), а не ispring_/stepik_:
    # площадка курса уже менялась, историю данных это ломать не должно.
    certificate_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True, default=None
    )
    certificate_status: Mapped[CertificateStatus] = mapped_column(
        SAEnum(CertificateStatus, name="certificate_status"),
        default=CertificateStatus.none,
        nullable=False,
    )
    certificate_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    certificate_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    certificate_reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    certificate_reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Первый уход на курс. Начало «слепой зоны» из KPI-документа: пока
    #: человек на внешней платформе, мы о нём ничего не знаем, и весь
    #: return rate считается от этой отметки.
    course_redirect_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    map_access_granted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(
        back_populates="volunteer", foreign_keys=[user_id]
    )


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
    #: joined, не select: /auth/me читает org синхронно из уже открытой
    #: async-сессии — implicit lazy load там роняет запрос в MissingGreenlet.
    organization: Mapped[Organization] = relationship(
        back_populates="staff_members", lazy="joined"
    )


# ---------------------------------------------------------------------------
# Hypotheses (экологические наблюдения волонтёров)
# ---------------------------------------------------------------------------

class Hypothesis(Base):
    __tablename__ = "hypotheses"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ---- P0-1: идемпотентность для офлайна ----
    # Мобильное приложение генерирует UUID на устройстве и
    # шлёт его вместе с точкой. Пара (author, client_id)
    # уникальна — повторный POST вернёт 200 вместо дубля.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, nullable=True,
    )
    #: Время создания на устройстве. Если старше серверного
    #: более чем на 5 мин — точка пришла из офлайн-очереди.
    created_at_client: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    lat: Mapped[float] = mapped_column(
        Float, nullable=False,
    )
    lon: Mapped[float] = mapped_column(
        Float, nullable=False,
    )

    # Точка наблюдения — PostGIS POINT из (lon, lat).
    location = mapped_column(
        Geometry(
            geometry_type="POINT",
            srid=4326,
            spatial_index=True,
        ),
        nullable=True,
    )

    # Полигон разлива / пятна загрязнения. Универсальный тип
    # GEOMETRY: волонтёр может обвести и точку, и полигон.
    geom = mapped_column(
        Geometry(
            geometry_type="GEOMETRY",
            srid=4326,
            spatial_index=False,
        ),
        nullable=True,
    )

    description: Mapped[str] = mapped_column(
        Text, nullable=False,
    )
    photo_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True,
    )

    # ---- Характеристики мусора ----
    # Проект изучает не только факт загрязнения, но и состав,
    # фракцию и объём — без них нельзя спрогнозировать затраты
    # на уборку и сравнить замеры на одной площадке.
    trash_categories: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(32)), nullable=True,
    )
    dominant_category: Mapped[TrashCategory | None] = mapped_column(
        SAEnum(TrashCategory, name="trash_category"),
        nullable=True,
    )
    fraction: Mapped[TrashFraction | None] = mapped_column(
        SAEnum(TrashFraction, name="trash_fraction"),
        nullable=True,
    )
    access_type: Mapped[AccessType | None] = mapped_column(
        SAEnum(AccessType, name="access_type"),
        nullable=True,
    )

    # Человек указывает либо объём, либо площадь пятна —
    # что проще оценить на месте.
    estimated_area_m2: Mapped[float | None] = mapped_column(
        Float, nullable=True,
    )
    estimated_volume_m3: Mapped[float | None] = mapped_column(
        Float, nullable=True,
    )

    # Производные величины. Хранятся, а не считаются на лету:
    # коэффициенты со временем поменяются, а смета, показанная
    # ООПТ, должна остаться той, по которой принимали решение.
    computed_volume_m3: Mapped[float | None] = mapped_column(
        Float, nullable=True,
    )
    computed_mass_kg: Mapped[float | None] = mapped_column(
        Float, nullable=True,
    )
    cleanup_cost_rub: Mapped[float | None] = mapped_column(
        Float, nullable=True,
    )
    cost_assumptions: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
    )

    # Замер на площадке многолетних наблюдений.
    monitoring_site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "monitoring_sites.id", ondelete="SET NULL",
        ),
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

    author: Mapped[User] = relationship(
        foreign_keys=[author_id], lazy="joined",
    )
    organization: Mapped[Organization | None] = relationship(
        back_populates="hypotheses", lazy="joined",
    )
    event: Mapped[Event | None] = relationship(
        back_populates="hypothesis",
        uselist=False,
        lazy="joined",
    )

    __table_args__ = (
        # P0-1: дедупликация офлайн-точек. Частичный уникальный
        # индекс: client_id заполнен только у мобильных клиентов,
        # веб может не слать его — NULL'ы не конфликтуют.
        UniqueConstraint(
            "author_id",
            "client_id",
            name="uq_hypotheses_author_client",
        ),
        Index(
            "idx_hypotheses_geom",
            "geom",
            postgresql_using="gist",
        ),
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


# ---------------------------------------------------------------------------
# Notifications — «допройди курс» и всё остальное
# ---------------------------------------------------------------------------

class Notification(Base):
    """Уведомление пользователю.

    Хранится в БД, а не отправляется и забывается, по двум причинам. Первая —
    in-app колокольчик. Вторая важнее: KPI «эффективность напоминаний» из
    KPI-документа требует связать отправку с возвратом. Для этого у каждого
    напоминания есть свой id, он же уходит в ссылку как `?nid=`, и клик по
    нему становится событием `reminder_clicked`.
    """

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[NotificationKind] = mapped_column(
        SAEnum(NotificationKind, name="notification_kind"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Куда ведёт уведомление внутри приложения.
    action_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    #: Ключ дедупликации напоминаний: user + вид + этап. У обычных
    #: уведомлений пуст — они не повторяются по расписанию.
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    clicked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", "created_at"),
        # Частичный индекс: колокольчик спрашивает только про непрочитанные,
        # а их всегда меньшинство.
        Index(
            "ix_notifications_unread",
            "user_id",
            postgresql_where=read_at.is_(None),
        ),
        Index(
            "uq_notifications_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=dedupe_key.isnot(None),
        ),
    )


# ---------------------------------------------------------------------------
# Company registry cache — сведения из ЕГРЮЛ по ИНН
# ---------------------------------------------------------------------------

class CompanyRegistryCache(Base):
    """Кэш ответов внешнего реестра.

    Не оптимизация, а развязка: ЕГРЮЛ — чужой сервис без гарантий доступности,
    и повторная регистрация или переоткрытие формы не должны от него зависеть.
    Пустой payload означает «спрашивали, организации нет» — отрицательный ответ
    кэшируется тоже, иначе перебор несуществующих ИНН превращается в поток
    запросов к ФНС от нашего имени.
    """

    __tablename__ = "company_registry_cache"

    inn: Mapped[str] = mapped_column(String(12), primary_key=True)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


# ---------------------------------------------------------------------------
# Parental consent — участие 14–17 лет
# ---------------------------------------------------------------------------

class ParentalConsent(Base):
    """Согласие законного представителя.

    Отдельная таблица, а не поля в volunteers: согласий может быть несколько
    (отказ → исправленное), и каждое — юридически значимый документ со своей
    датой и проверяющим. Историю таких документов затирать нельзя.
    """

    __tablename__ = "parental_consents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    volunteer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("volunteers.id", ondelete="CASCADE"), nullable=False, index=True
    )

    representative_name: Mapped[str] = mapped_column(String(256), nullable=False)
    representative_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    representative_email: Mapped[str] = mapped_column(String(320), nullable=False)
    #: Родство: мать / отец / опекун. Свободной строкой — перечень в законе
    #: шире, чем кажется, а нам это поле нужно только для человека-проверяющего.
    relation: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Скан подписанного согласия.
    scan_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    status: Mapped[ConsentStatus] = mapped_column(
        SAEnum(ConsentStatus, name="consent_status"),
        default=ConsentStatus.awaiting,
        nullable=False,
    )
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    volunteer: Mapped[Volunteer] = relationship(lazy="joined")

    __table_args__ = (
        Index("ix_parental_consents_status_submitted", "status", "submitted_at"),
    )


# ---------------------------------------------------------------------------
# Cadastral parcels — участки ООПТ
# ---------------------------------------------------------------------------

class CadastralParcel(Base):
    """Кадастровый участок организации.

    Заменяет единственное поле `Organization.cadastral_number`: у ООПТ участков
    почти всегда несколько — Кроноцкий заповедник и Южно-Камчатский заказник
    состоят из десятков кластеров, а «Русская Арктика» разбросана по островам.

    Геометрия приходит из Росреестра асинхронно и может не прийти вовсе —
    поэтому статус отдельным полем, а участок существует и без границ.
    """

    __tablename__ = "cadastral_parcels"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cadastral_number: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )

    # MultiPolygon, а не Polygon: участок бывает многоконтурным.
    geom = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )
    area_ha: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[ParcelStatus] = mapped_column(
        SAEnum(ParcelStatus, name="parcel_status"),
        default=ParcelStatus.pending,
        nullable=False,
    )
    #: rosreestr / manual — откуда взялись границы. Важно на защите: часть
    #: участков придётся вводить руками, и это должно быть видно в данных.
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolve_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    organization: Mapped[Organization] = relationship(lazy="joined")

    __table_args__ = (
        Index("idx_cadastral_parcels_geom", "geom", postgresql_using="gist"),
        Index("ix_cadastral_parcels_status", "status"),
    )
