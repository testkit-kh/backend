"""0009: P1-4 мероприятия по уборке + P1-5 лента «Мои точки».

hypothesis_status:
- новое значение `cleaned` — точка убрана (ставится закрытием мероприятия)

hypotheses:
- reject_reason — причина отказа для ленты «Мои точки»

events:
- description / place / scheduled_at — планирование выезда (PATCH)
- completed_at / actual_participants / waste_volume_m3 / waste_mass_kg /
  result_notes — итоги уборки
- updated_at — мероприятие теперь редактируется

Новые таблицы:
- event_participants — записи волонтёров на мероприятия

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-05
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_HYPOTHESIS_STATUS_OLD = (
    "pending",
    "approved",
    "rejected",
    "drone_requested",
)


def upgrade() -> None:
    # ---- hypothesis_status += 'cleaned' ------------------------------------
    # autocommit_block: ALTER TYPE ... ADD VALUE в транзакции запрещён
    # в PostgreSQL < 12, а в 12+ добавленное значение всё равно нельзя
    # использовать до коммита. Отдельный коммит снимает оба ограничения.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE hypothesis_status ADD VALUE IF NOT EXISTS 'cleaned'"
        )

    # ---- hypotheses.reject_reason ------------------------------------------
    op.add_column(
        "hypotheses",
        sa.Column("reject_reason", sa.Text(), nullable=True),
    )

    # ---- events: планирование выезда --------------------------------------
    op.add_column("events", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("events", sa.Column("place", sa.String(512), nullable=True))
    op.add_column(
        "events",
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ---- events: итоги уборки ---------------------------------------------
    op.add_column(
        "events",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("actual_participants", sa.Integer(), nullable=True),
    )
    op.add_column("events", sa.Column("waste_volume_m3", sa.Float(), nullable=True))
    op.add_column("events", sa.Column("waste_mass_kg", sa.Float(), nullable=True))
    op.add_column("events", sa.Column("result_notes", sa.Text(), nullable=True))

    # ---- events.updated_at -------------------------------------------------
    # Добавляем nullable, засеваем из created_at, затем NOT NULL: у уже
    # существующих мероприятий правок не было, и момент «последнего
    # изменения» для них равен моменту создания.
    op.add_column(
        "events",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE events SET updated_at = created_at WHERE updated_at IS NULL")
    op.alter_column("events", "updated_at", nullable=False)

    # ---- event_participants ------------------------------------------------
    op.create_table(
        "event_participants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attended",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # Идемпотентность POST /join на уровне БД: второй записи того же
        # волонтёра на то же мероприятие не появится даже при гонке.
        sa.UniqueConstraint(
            "event_id", "user_id", name="uq_event_participants_event_user"
        ),
    )
    op.create_index("ix_event_participants_event_id", "event_participants", ["event_id"])
    op.create_index("ix_event_participants_user_id", "event_participants", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_event_participants_user_id", table_name="event_participants")
    op.drop_index("ix_event_participants_event_id", table_name="event_participants")
    op.drop_table("event_participants")

    op.drop_column("events", "updated_at")
    op.drop_column("events", "result_notes")
    op.drop_column("events", "waste_mass_kg")
    op.drop_column("events", "waste_volume_m3")
    op.drop_column("events", "actual_participants")
    op.drop_column("events", "completed_at")
    op.drop_column("events", "scheduled_at")
    op.drop_column("events", "place")
    op.drop_column("events", "description")

    op.drop_column("hypotheses", "reject_reason")

    # ---- hypothesis_status -= 'cleaned' -----------------------------------
    # Значение из enum в PostgreSQL не удаляется — тип пересоздаётся.
    # Убранные точки при откате становятся approved: это ближайший
    # существовавший статус, а терять их совсем нельзя.
    old_values = ", ".join(f"'{v}'" for v in _HYPOTHESIS_STATUS_OLD)
    op.execute(
        "UPDATE hypotheses SET status = 'approved' WHERE status = 'cleaned'"
    )
    op.execute("ALTER TABLE hypotheses ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE hypothesis_status RENAME TO hypothesis_status_old")
    op.execute(f"CREATE TYPE hypothesis_status AS ENUM ({old_values})")
    op.execute(
        "ALTER TABLE hypotheses ALTER COLUMN status TYPE hypothesis_status"
        " USING status::text::hypothesis_status"
    )
    op.execute(
        "ALTER TABLE hypotheses ALTER COLUMN status SET DEFAULT 'pending'"
    )
    op.execute("DROP TYPE hypothesis_status_old")
