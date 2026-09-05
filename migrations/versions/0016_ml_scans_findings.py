"""0016: ML scans/findings + hypothesis.source для uav_auto.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

hypothesis_source = postgresql.ENUM(
    "manual",
    "uav_auto",
    name="hypothesis_source",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM("manual", "uav_auto", name="hypothesis_source").create(
        bind, checkfirst=True
    )

    op.add_column(
        "hypotheses",
        sa.Column(
            "source",
            hypothesis_source,
            nullable=False,
            server_default="manual",
        ),
    )
    op.create_index("ix_hypotheses_source", "hypotheses", ["source"])

    op.create_table(
        "ml_scans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("requester_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("bbox", postgresql.JSONB(), nullable=False),
        sa.Column("zoom", sa.Integer(), nullable=False),
        sa.Column("tile_source", sa.String(64), nullable=True),
        sa.Column("ml_job_id", sa.String(64), nullable=True),
        sa.Column("summary", postgresql.JSONB(), nullable=True),
        sa.Column("geojson", postgresql.JSONB(), nullable=True),
        sa.Column("overlay_bounds", postgresql.JSONB(), nullable=True),
        sa.Column("imagery", postgresql.JSONB(), nullable=True),
        sa.Column("fraud_flags", postgresql.JSONB(), nullable=True),
        sa.Column("model_info", postgresql.JSONB(), nullable=True),
        sa.Column(
            "candidates_suppressed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["requester_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_ml_scans_requester_id", "ml_scans", ["requester_id"])
    op.create_index("ix_ml_scans_organization_id", "ml_scans", ["organization_id"])
    op.create_index("ix_ml_scans_created_at", "ml_scans", ["created_at"])

    op.create_table(
        "ml_findings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scan_id", sa.Uuid(), nullable=False),
        sa.Column("detection_id", sa.Integer(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="GEOMETRY",
                srid=4326,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column(
            "trash_categories",
            postgresql.ARRAY(sa.String(32)),
            nullable=True,
        ),
        sa.Column("dominant_category", sa.String(32), nullable=True),
        sa.Column("fraction", sa.String(16), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("estimated_volume_m3", sa.Float(), nullable=True),
        sa.Column("estimated_mass_kg", sa.Float(), nullable=True),
        sa.Column("label_ru", sa.String(128), nullable=True),
        sa.Column("color_hex", sa.String(16), nullable=True),
        sa.Column("hypothesis_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["scan_id"], ["ml_scans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"], ["hypotheses.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_ml_findings_scan_id", "ml_findings", ["scan_id"])
    op.create_index("ix_ml_findings_hypothesis_id", "ml_findings", ["hypothesis_id"])
    op.create_index("ix_ml_findings_created_at", "ml_findings", ["created_at"])
    op.create_index(
        "idx_ml_findings_geom",
        "ml_findings",
        ["geom"],
        postgresql_using="gist",
    )

    # Precision автодетекции: доля uav_auto, подтверждённых человеком.
    op.execute(
        """
        CREATE OR REPLACE VIEW kpi.autodetect_precision AS
        SELECT
            COUNT(*) FILTER (WHERE status IN ('approved', 'cleaned', 'drone_requested'))
                AS approved_count,
            COUNT(*) AS total_count,
            CASE
                WHEN COUNT(*) = 0 THEN NULL
                ELSE COUNT(*) FILTER (
                    WHERE status IN ('approved', 'cleaned', 'drone_requested')
                )::float / COUNT(*)::float
            END AS precision
        FROM hypotheses
        WHERE source = 'uav_auto'
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS kpi.autodetect_precision")
    op.drop_index("idx_ml_findings_geom", table_name="ml_findings")
    op.drop_index("ix_ml_findings_created_at", table_name="ml_findings")
    op.drop_index("ix_ml_findings_hypothesis_id", table_name="ml_findings")
    op.drop_index("ix_ml_findings_scan_id", table_name="ml_findings")
    op.drop_table("ml_findings")
    op.drop_index("ix_ml_scans_created_at", table_name="ml_scans")
    op.drop_index("ix_ml_scans_organization_id", table_name="ml_scans")
    op.drop_index("ix_ml_scans_requester_id", table_name="ml_scans")
    op.drop_table("ml_scans")
    op.drop_index("ix_hypotheses_source", table_name="hypotheses")
    op.drop_column("hypotheses", "source")
    postgresql.ENUM(name="hypothesis_source").drop(op.get_bind(), checkfirst=True)
