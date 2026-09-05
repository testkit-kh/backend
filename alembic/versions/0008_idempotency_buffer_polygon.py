"""0008: P0-1 идемпотентность + P0-3 буферная зона + полигон.

Новые колонки в hypotheses:
- client_id / created_at_client  — офлайн-идемпотентность
- geom (Geometry)                — полигон разлива

Новые индексы:
- uq_hypotheses_author_client    — дедупликация офлайн-точек
- idx_hypotheses_geom            — GIST для пространственных запросов

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-05
"""

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry


# revision identifiers
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- client_id: UUID, nullable --
    # Мобильный клиент генерирует его на устройстве.
    # NULL допустим — веб-клиент может не слать.
    op.add_column(
        "hypotheses",
        sa.Column(
            "client_id",
            sa.Uuid(),
            nullable=True,
        ),
    )

    # -- created_at_client: timestamptz, nullable --
    # Нужен для определения offline-режима:
    # если разница с серверным > 5 мин — точка
    # пришла из очереди.
    op.add_column(
        "hypotheses",
        sa.Column(
            "created_at_client",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # -- geom: универсальная геометрия (GEOMETRY, 4326) --
    # Не POLYGON, а GEOMETRY: волонтёр может обвести
    # и точку, и произвольный полигон разлива.
    op.add_column(
        "hypotheses",
        sa.Column(
            "geom",
            Geometry(
                geometry_type="GEOMETRY",
                srid=4326,
                spatial_index=False,
            ),
            nullable=True,
        ),
    )

    # -- Уникальный констрейнт (author_id, client_id) --
    # Частичный: NULL'ы не конфликтуют в PostgreSQL,
    # поэтому веб-запросы без client_id проходят свободно.
    op.create_unique_constraint(
        "uq_hypotheses_author_client",
        "hypotheses",
        ["author_id", "client_id"],
    )

    # -- GIST-индекс на geom для пространственных запросов --
    op.create_index(
        "idx_hypotheses_geom",
        "hypotheses",
        ["geom"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_hypotheses_geom",
        table_name="hypotheses",
    )
    op.drop_constraint(
        "uq_hypotheses_author_client",
        "hypotheses",
        type_="unique",
    )
    op.drop_column("hypotheses", "geom")
    op.drop_column("hypotheses", "created_at_client")
    op.drop_column("hypotheses", "client_id")
