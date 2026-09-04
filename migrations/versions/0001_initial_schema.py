"""Initial schema: PostGIS, users, volunteers, organizations, staff, hypotheses, events

Revision ID: 0001
Revises:
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


user_role = sa.Enum("volunteer", "staff", name="user_role")
org_verification_status = sa.Enum(
    "pending", "verified", "failed", "manual_review", name="org_verification_status"
)
hypothesis_status = sa.Enum(
    "pending", "approved", "rejected", "drone_requested", name="hypothesis_status"
)
event_status = sa.Enum(
    "planned", "in_progress", "completed", "cancelled", name="event_status"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    bind = op.get_bind()
    user_role.create(bind, checkfirst=True)
    org_verification_status.create(bind, checkfirst=True)
    hypothesis_status.create(bind, checkfirst=True)
    event_status.create(bind, checkfirst=True)

    # ---- users -----------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("full_name", sa.String(256), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # ---- volunteers ------------------------------------------------------
    op.create_table(
        "volunteers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "is_trained", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("is_over_14", sa.Boolean(), nullable=False),
        sa.Column("stepik_cert_url", sa.String(2048), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_volunteers_user_id"),
    )

    # ---- organizations ---------------------------------------------------
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("cadastral_number", sa.String(64), nullable=True),
        sa.Column(
            "territory_geom",
            geoalchemy2.types.Geometry(
                geometry_type="POLYGON",
                srid=4326,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column(
            "verification_status",
            org_verification_status,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("inn", name="uq_organizations_inn"),
    )
    op.create_index("ix_organizations_inn", "organizations", ["inn"])
    op.create_index(
        "idx_organizations_territory_geom",
        "organizations",
        ["territory_geom"],
        postgresql_using="gist",
    )

    # ---- staff -----------------------------------------------------------
    op.create_table(
        "staff",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("user_id", name="uq_staff_user_id"),
    )

    # ---- hypotheses ------------------------------------------------------
    op.create_table(
        "hypotheses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column(
            "location",
            geoalchemy2.types.Geometry(
                geometry_type="POINT",
                srid=4326,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("photo_url", sa.String(2048), nullable=True),
        sa.Column(
            "status", hypothesis_status, nullable=False, server_default="pending"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_hypotheses_author_id", "hypotheses", ["author_id"])
    op.create_index("ix_hypotheses_organization_id", "hypotheses", ["organization_id"])
    op.create_index(
        "idx_hypotheses_location", "hypotheses", ["location"], postgresql_using="gist"
    )

    # ---- events (domain: cleanup events spawned from approved hypotheses) --
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("hypothesis_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("status", event_status, nullable=False, server_default="planned"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"], ["hypotheses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("hypothesis_id", name="uq_events_hypothesis_id"),
    )


def downgrade() -> None:
    op.drop_table("events")
    op.drop_index("idx_hypotheses_location", table_name="hypotheses")
    op.drop_table("hypotheses")
    op.drop_table("staff")
    op.drop_index("idx_organizations_territory_geom", table_name="organizations")
    op.drop_table("organizations")
    op.drop_table("volunteers")
    op.drop_table("users")

    bind = op.get_bind()
    event_status.drop(bind, checkfirst=True)
    hypothesis_status.drop(bind, checkfirst=True)
    org_verification_status.drop(bind, checkfirst=True)
    user_role.drop(bind, checkfirst=True)
