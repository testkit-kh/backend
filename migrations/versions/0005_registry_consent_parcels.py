"""Registry cache, parental consent, cadastral parcels

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONSENT_STATUS = ("not_required", "awaiting", "approved", "rejected")
PARCEL_STATUS = ("pending", "resolved", "failed")
NOTIFICATION_KIND = (
    "consent_required",
    "consent_approved",
    "consent_rejected",
    "course_not_started",
    "course_not_finished",
    "certificate_approved",
    "certificate_rejected",
    "point_validated",
    "cleanup_event_invite",
)

consent_status = postgresql.ENUM(
    *CONSENT_STATUS, name="consent_status", create_type=False
)
parcel_status = postgresql.ENUM(*PARCEL_STATUS, name="parcel_status", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*CONSENT_STATUS, name="consent_status").create(bind, checkfirst=True)
    postgresql.ENUM(*PARCEL_STATUS, name="parcel_status").create(bind, checkfirst=True)

    # ---- notification_kind: три новых значения -----------------------------
    # Пересоздаём тип целиком: ALTER TYPE ... ADD VALUE нельзя использовать в
    # той же транзакции, в которой значение добавлено, а Alembic держит
    # миграцию одной транзакцией.
    op.execute("ALTER TYPE notification_kind RENAME TO notification_kind_old")
    postgresql.ENUM(*NOTIFICATION_KIND, name="notification_kind").create(bind)
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN kind TYPE notification_kind "
        "USING kind::text::notification_kind"
    )
    op.execute("DROP TYPE notification_kind_old")

    # ---- volunteers: дата рождения и согласие ------------------------------
    op.add_column("volunteers", sa.Column("birth_date", sa.Date(), nullable=True))
    op.add_column(
        "volunteers",
        sa.Column(
            "consent_status",
            consent_status,
            nullable=False,
            server_default="not_required",
        ),
    )

    # ---- company_registry_cache -------------------------------------------
    op.create_table(
        "company_registry_cache",
        sa.Column("inn", sa.String(12), primary_key=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ---- parental_consents -------------------------------------------------
    op.create_table(
        "parental_consents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("volunteer_id", sa.Uuid(), nullable=False),
        sa.Column("representative_name", sa.String(256), nullable=False),
        sa.Column("representative_phone", sa.String(32), nullable=False),
        sa.Column("representative_email", sa.String(320), nullable=False),
        sa.Column("relation", sa.String(64), nullable=True),
        sa.Column("scan_url", sa.String(2048), nullable=True),
        sa.Column(
            "status", consent_status, nullable=False, server_default="awaiting"
        ),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["volunteer_id"], ["volunteers.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_parental_consents_volunteer_id", "parental_consents", ["volunteer_id"]
    )
    op.create_index(
        "ix_parental_consents_status_submitted",
        "parental_consents",
        ["status", "submitted_at"],
    )

    # ---- cadastral_parcels -------------------------------------------------
    op.create_table(
        "cadastral_parcels",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("cadastral_number", sa.String(64), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="MULTIPOLYGON",
                srid=4326,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column("area_ha", sa.Float(), nullable=True),
        sa.Column("status", parcel_status, nullable=False, server_default="pending"),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column("resolve_error", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "cadastral_number", name="uq_cadastral_parcels_cadastral_number"
        ),
    )
    op.create_index(
        "ix_cadastral_parcels_organization_id", "cadastral_parcels", ["organization_id"]
    )
    op.create_index("ix_cadastral_parcels_status", "cadastral_parcels", ["status"])
    op.create_index(
        "idx_cadastral_parcels_geom",
        "cadastral_parcels",
        ["geom"],
        postgresql_using="gist",
    )

    # Переносим единственный кадастровый номер организации в новую таблицу,
    # чтобы уже заведённые ООПТ не потеряли его при переходе на 1→N.
    op.execute(
        """
        INSERT INTO cadastral_parcels (id, organization_id, cadastral_number, status, created_at)
        SELECT gen_random_uuid(), id, cadastral_number, 'pending', now()
        FROM organizations
        WHERE cadastral_number IS NOT NULL AND cadastral_number <> ''
        ON CONFLICT (cadastral_number) DO NOTHING
        """
    )

    # ---- territory_geom: POLYGON → MULTIPOLYGON ----------------------------
    # Территория теперь собирается объединением участков, а оно почти всегда
    # многоконтурное: Кроноцкий и «Русская Арктика» состоят из кластеров.
    op.execute(
        "ALTER TABLE organizations "
        "ALTER COLUMN territory_geom TYPE geometry(MultiPolygon, 4326) "
        "USING ST_Multi(territory_geom)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE organizations "
        "ALTER COLUMN territory_geom TYPE geometry(Polygon, 4326) "
        "USING ST_GeometryN(territory_geom, 1)"
    )

    op.drop_index("idx_cadastral_parcels_geom", table_name="cadastral_parcels")
    op.drop_index("ix_cadastral_parcels_status", table_name="cadastral_parcels")
    op.drop_index(
        "ix_cadastral_parcels_organization_id", table_name="cadastral_parcels"
    )
    op.drop_table("cadastral_parcels")

    op.drop_index(
        "ix_parental_consents_status_submitted", table_name="parental_consents"
    )
    op.drop_index("ix_parental_consents_volunteer_id", table_name="parental_consents")
    op.drop_table("parental_consents")

    op.drop_table("company_registry_cache")

    op.drop_column("volunteers", "consent_status")
    op.drop_column("volunteers", "birth_date")

    bind = op.get_bind()
    op.execute(
        "UPDATE notifications SET kind = 'course_not_finished' "
        "WHERE kind::text IN ('consent_required','consent_approved','consent_rejected')"
    )
    op.execute("ALTER TYPE notification_kind RENAME TO notification_kind_old")
    postgresql.ENUM(
        "course_not_started",
        "course_not_finished",
        "certificate_approved",
        "certificate_rejected",
        "point_validated",
        "cleanup_event_invite",
        name="notification_kind",
    ).create(bind)
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN kind TYPE notification_kind "
        "USING kind::text::notification_kind"
    )
    op.execute("DROP TYPE notification_kind_old")

    postgresql.ENUM(name="parcel_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="consent_status").drop(bind, checkfirst=True)
