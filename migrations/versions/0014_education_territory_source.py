"""0014: анкета образования + источник границ территории.

volunteer_education — одна строка на волонтёра (unique volunteer_id).
organizations.territory_source / territory_osm_id — чтобы OSM-границу
не путали с выпиской ЕГРН.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-05
"""

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    education_level = sa.Enum(
        "school",
        "college",
        "university",
        "working",
        "other",
        name="education_level",
    )

    op.create_table(
        "volunteer_education",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("volunteer_id", sa.Uuid(), nullable=False),
        sa.Column("level", education_level, nullable=False),
        sa.Column("institution_name", sa.String(length=512), nullable=True),
        sa.Column("institution_inn", sa.String(length=12), nullable=True),
        sa.Column("registry_name", sa.String(length=512), nullable=True),
        sa.Column("grade", sa.String(length=32), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["volunteer_id"], ["volunteers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("volunteer_id", name="uq_volunteer_education_volunteer"),
    )

    op.add_column(
        "organizations", sa.Column("territory_source", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "organizations", sa.Column("territory_osm_id", sa.String(length=64), nullable=True)
    )
    op.execute(
        """
        UPDATE organizations
        SET territory_source = 'egrn'
        WHERE territory_geom IS NOT NULL AND territory_source IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("organizations", "territory_osm_id")
    op.drop_column("organizations", "territory_source")
    op.drop_table("volunteer_education")
    sa.Enum(name="education_level").drop(op.get_bind(), checkfirst=True)
