"""0018: satellite_scenes (Sentinel-2 STAC) + hypothesis.source=satellite_auto.

satellite_scenes:
- сцены Sentinel-2, найденные через STAC (Element84 Earth Search) и
  сохранённые вместе со всеми asset-ссылками (COG в публичном S3).

hypothesis_source:
- новое значение `satellite_auto` — кандидат от спектрального детектора
  (NDWI/NDTI) поверх сцены Sentinel-2, POST /api/v1/satellite/detect.

kpi.autodetect_precision_satellite:
- зеркало kpi.autodetect_precision (0016), но по source='satellite_auto'.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- hypothesis_source += 'satellite_auto' ------------------------------
    # autocommit_block: ALTER TYPE ... ADD VALUE нельзя внутри обычной
    # транзакции — новое значение недоступно до коммита (см. 0009).
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE hypothesis_source ADD VALUE IF NOT EXISTS 'satellite_auto'"
        )

    # ---- satellite_scenes ----------------------------------------------------
    op.create_table(
        "satellite_scenes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("stac_id", sa.String(128), nullable=False),
        sa.Column("collection", sa.String(64), nullable=False),
        sa.Column("datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cloud_cover", sa.Float(), nullable=True),
        sa.Column("bbox", postgresql.JSONB(), nullable=False),
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
        sa.Column("assets", postgresql.JSONB(), nullable=False),
        sa.Column("thumbnail_url", sa.String(2048), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_satellite_scenes_stac_id", "satellite_scenes", ["stac_id"], unique=True
    )
    op.create_index(
        "ix_satellite_scenes_datetime", "satellite_scenes", ["datetime"]
    )
    op.create_index(
        "ix_satellite_scenes_organization_id",
        "satellite_scenes",
        ["organization_id"],
    )
    op.create_index(
        "idx_satellite_scenes_geom",
        "satellite_scenes",
        ["geom"],
        postgresql_using="gist",
    )

    # Precision автодетекции по спутнику: доля satellite_auto, подтверждённых
    # человеком. Зеркало kpi.autodetect_precision (0016) под другой source.
    op.execute(
        """
        CREATE OR REPLACE VIEW kpi.autodetect_precision_satellite AS
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
        WHERE source = 'satellite_auto'
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS kpi.autodetect_precision_satellite")

    op.drop_index("idx_satellite_scenes_geom", table_name="satellite_scenes")
    op.drop_index(
        "ix_satellite_scenes_organization_id", table_name="satellite_scenes"
    )
    op.drop_index("ix_satellite_scenes_datetime", table_name="satellite_scenes")
    op.drop_index("ix_satellite_scenes_stac_id", table_name="satellite_scenes")
    op.drop_table("satellite_scenes")

    # ---- hypothesis_source -= 'satellite_auto' ------------------------------
    # Значение из enum не удаляется — тип пересоздаётся, а это ALTER COLUMN
    # TYPE на hypotheses.source. kpi.autodetect_precision (0016) — вьюха
    # поверх этой же колонки, и PostgreSQL отказывает в ALTER COLUMN TYPE,
    # пока на колонку есть зависимая _RETURN rule (проверено вживую при
    # написании этой миграции: "cannot alter type of a column used by a
    # view or rule"). Поэтому вьюху пересоздаём вокруг каста — дропаем перед
    # ним и восстанавливаем ровно тем же телом, что и в 0016, сразу после.
    op.execute("DROP VIEW IF EXISTS kpi.autodetect_precision")

    op.execute(
        "UPDATE hypotheses SET source = 'manual' WHERE source = 'satellite_auto'"
    )
    op.execute(
        "ALTER TABLE hypotheses ALTER COLUMN source DROP DEFAULT"
    )
    op.execute("ALTER TYPE hypothesis_source RENAME TO hypothesis_source_old")
    op.execute("CREATE TYPE hypothesis_source AS ENUM ('manual', 'uav_auto')")
    op.execute(
        "ALTER TABLE hypotheses ALTER COLUMN source TYPE hypothesis_source"
        " USING source::text::hypothesis_source"
    )
    op.execute(
        "ALTER TABLE hypotheses ALTER COLUMN source SET DEFAULT 'manual'"
    )
    op.execute("DROP TYPE hypothesis_source_old")

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
