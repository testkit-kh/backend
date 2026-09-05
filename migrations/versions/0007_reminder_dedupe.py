"""Reminder de-duplication key on notifications

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-05

Планировщик запускается по расписанию и на нескольких репликах API сразу.
Без ключа дедупликации один и тот же человек получил бы «допройди курс»
столько раз, сколько тиков успело пройти, — и это не только раздражает, но и
ломает KPI: `reminder_sent` считается по событиям, и накрутка отправок
занизила бы конверсию напоминаний.

Уникальный индекс здесь — последняя линия обороны. Первая — условие в самом
запросе поиска адресатов; но между выборкой и вставкой на двух репликах есть
гонка, и закрыть её может только БД.
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("dedupe_key", sa.String(128), nullable=True))
    # Частичный уникальный индекс: обычные уведомления ключа не имеют и
    # ограничением не связаны, а напоминания — связаны жёстко.
    op.create_index(
        "uq_notifications_dedupe_key",
        "notifications",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_notifications_dedupe_key", table_name="notifications")
    op.drop_column("notifications", "dedupe_key")
