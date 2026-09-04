"""Trash composition/volume/cost on hypotheses + long-term monitoring sites

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TRASH_CATEGORY = (
    "plastic",
    "fishing_gear",
    "glass",
    "metal",
    "wood",
    "rubber",
    "hazardous",
    "household",
    "construction",
    "other",
)
TRASH_FRACTION = ("mega", "macro", "meso", "micro")
ACCESS_TYPE = ("on_foot", "vehicle", "boat", "helicopter")

# Типы создаются один раз явно: они используются в двух таблицах, и если
# отдать это на откуп sa.Enum внутри create_table, вторая таблица упадёт на
# "type already exists".
trash_category = postgresql.ENUM(*TRASH_CATEGORY, name="trash_category", create_type=False)
trash_fraction = postgresql.ENUM(*TRASH_FRACTION, name="trash_fraction", create_type=False)
access_type = postgresql.ENUM(*ACCESS_TYPE, name="access_type", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*TRASH_CATEGORY, name="trash_category").create(bind, checkfirst=True)
    postgresql.ENUM(*TRASH_FRACTION, name="trash_fraction").create(bind, checkfirst=True)
    postgresql.ENUM(*ACCESS_TYPE, name="access_type").create(bind, checkfirst=True)

    # ---- monitoring_sites -------------------------------------------------
    op.create_table(
        "monitoring_sites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="POLYGON",
                srid=4326,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column("area_m2", sa.Float(), nullable=True),
        sa.Column("shoreline_length_m", sa.Float(), nullable=True),
        sa.Column("established_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("protocol", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("code", name="uq_monitoring_sites_code"),
    )
    op.create_index(
        "ix_monitoring_sites_organization_id", "monitoring_sites", ["organization_id"]
    )
    op.create_index(
        "idx_monitoring_sites_geom", "monitoring_sites", ["geom"], postgresql_using="gist"
    )

    # ---- site_surveys -----------------------------------------------------
    op.create_table(
        "site_surveys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("surveyed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trash_categories", postgresql.ARRAY(sa.String(32)), nullable=True),
        sa.Column("dominant_category", trash_category, nullable=True),
        sa.Column("fraction", trash_fraction, nullable=True),
        sa.Column("item_count", sa.BigInteger(), nullable=True),
        sa.Column("estimated_area_m2", sa.Float(), nullable=True),
        sa.Column("estimated_volume_m3", sa.Float(), nullable=True),
        sa.Column("computed_volume_m3", sa.Float(), nullable=True),
        sa.Column("computed_mass_kg", sa.Float(), nullable=True),
        sa.Column(
            "was_cleaned", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("photo_urls", postgresql.ARRAY(sa.String(2048)), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["monitoring_sites.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_site_surveys_site_id", "site_surveys", ["site_id"])
    op.create_index(
        "ix_site_surveys_site_surveyed", "site_surveys", ["site_id", "surveyed_at"]
    )

    # ---- hypotheses: trash details ----------------------------------------
    op.add_column(
        "hypotheses",
        sa.Column("trash_categories", postgresql.ARRAY(sa.String(32)), nullable=True),
    )
    op.add_column("hypotheses", sa.Column("dominant_category", trash_category, nullable=True))
    op.add_column("hypotheses", sa.Column("fraction", trash_fraction, nullable=True))
    op.add_column("hypotheses", sa.Column("access_type", access_type, nullable=True))
    op.add_column("hypotheses", sa.Column("estimated_area_m2", sa.Float(), nullable=True))
    op.add_column("hypotheses", sa.Column("estimated_volume_m3", sa.Float(), nullable=True))
    op.add_column("hypotheses", sa.Column("computed_volume_m3", sa.Float(), nullable=True))
    op.add_column("hypotheses", sa.Column("computed_mass_kg", sa.Float(), nullable=True))
    op.add_column("hypotheses", sa.Column("cleanup_cost_rub", sa.Float(), nullable=True))
    op.add_column(
        "hypotheses",
        sa.Column("cost_assumptions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("hypotheses", sa.Column("monitoring_site_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_hypotheses_monitoring_site_id",
        "hypotheses",
        "monitoring_sites",
        ["monitoring_site_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_hypotheses_monitoring_site_id", "hypotheses", ["monitoring_site_id"]
    )
    # Поиск «все точки, где есть рыболовные снасти» — по GIN на массиве.
    op.create_index(
        "ix_hypotheses_trash_categories",
        "hypotheses",
        ["trash_categories"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_hypotheses_trash_categories", table_name="hypotheses")
    op.drop_index("ix_hypotheses_monitoring_site_id", table_name="hypotheses")
    op.drop_constraint(
        "fk_hypotheses_monitoring_site_id", "hypotheses", type_="foreignkey"
    )
    for column in (
        "monitoring_site_id",
        "cost_assumptions",
        "cleanup_cost_rub",
        "computed_mass_kg",
        "computed_volume_m3",
        "estimated_volume_m3",
        "estimated_area_m2",
        "access_type",
        "fraction",
        "dominant_category",
        "trash_categories",
    ):
        op.drop_column("hypotheses", column)

    op.drop_index("ix_site_surveys_site_surveyed", table_name="site_surveys")
    op.drop_index("ix_site_surveys_site_id", table_name="site_surveys")
    op.drop_table("site_surveys")

    op.drop_index("idx_monitoring_sites_geom", table_name="monitoring_sites")
    op.drop_index("ix_monitoring_sites_organization_id", table_name="monitoring_sites")
    op.drop_table("monitoring_sites")

    bind = op.get_bind()
    postgresql.ENUM(name="access_type").drop(bind, checkfirst=True)
    postgresql.ENUM(name="trash_fraction").drop(bind, checkfirst=True)
    postgresql.ENUM(name="trash_category").drop(bind, checkfirst=True)
