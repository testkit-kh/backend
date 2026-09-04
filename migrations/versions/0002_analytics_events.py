"""Analytics event log — the single source for every KPI

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "geo",
            geoalchemy2.types.Geography(
                geometry_type="POINT",
                srid=4326,
                spatial_index=False,
                from_text="ST_GeogFromText",
                name="geography",
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Deleting a user must not delete their history: the row is kept and
        # anonymised. This is what makes "right to be forgotten" compatible
        # with the funnel KPIs.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )

    op.create_index(
        "ix_analytics_events_type_created",
        "analytics_events",
        ["event_type", "created_at"],
    )
    op.create_index(
        "ix_analytics_events_user_created",
        "analytics_events",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_analytics_events_payload",
        "analytics_events",
        ["payload"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_analytics_events_geo",
        "analytics_events",
        ["geo"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("ix_analytics_events_geo", table_name="analytics_events")
    op.drop_index("ix_analytics_events_payload", table_name="analytics_events")
    op.drop_index("ix_analytics_events_user_created", table_name="analytics_events")
    op.drop_index("ix_analytics_events_type_created", table_name="analytics_events")
    op.drop_table("analytics_events")
