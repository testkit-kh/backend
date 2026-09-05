"""0017: standalone events (nullable hypothesis_id) + partial unique.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Мероприятие можно создать без гипотезы (сотрудник ООПТ вручную).
    # UNIQUE(hypothesis_id) оставляем: у PostgreSQL несколько NULL допустимы.
    op.alter_column(
        "events",
        "hypothesis_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    # Строки с NULL hypothesis_id нельзя вернуть в NOT NULL без удаления.
    op.execute("DELETE FROM events WHERE hypothesis_id IS NULL")
    op.alter_column(
        "events",
        "hypothesis_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
