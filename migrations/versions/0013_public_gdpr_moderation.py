"""0013: P3 — публичная карта, 152-ФЗ, журнал модерации.

hypotheses.author_id и event_participants.user_id:
- CASCADE заменён на SET NULL, колонки nullable.
  Удаление аккаунта больше не уносит точку и не стирает явку.

moderation_log:
- append-only таблица. Триггер запрещает UPDATE и DELETE.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-05
"""

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("hypotheses_author_id_fkey", "hypotheses", type_="foreignkey")
    op.alter_column("hypotheses", "author_id", existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key(
        "hypotheses_author_id_fkey",
        "hypotheses",
        "users",
        ["author_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint("event_participants_user_id_fkey", "event_participants", type_="foreignkey")
    op.alter_column("event_participants", "user_id", existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key(
        "event_participants_user_id_fkey",
        "event_participants",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "moderation_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_moderation_log_actor_id", "moderation_log", ["actor_id"])
    op.create_index("ix_moderation_log_entity_id", "moderation_log", ["entity_id"])

    # Журнал неизменяем на уровне БД: обойти это через ORM недостаточно.
    op.execute(
        """
        CREATE FUNCTION moderation_log_forbid_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'moderation_log is append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER moderation_log_no_mutation
        BEFORE UPDATE OR DELETE ON moderation_log
        FOR EACH ROW
        EXECUTE FUNCTION moderation_log_forbid_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS moderation_log_no_mutation ON moderation_log")
    op.execute("DROP FUNCTION IF EXISTS moderation_log_forbid_mutation()")
    op.drop_index("ix_moderation_log_entity_id", table_name="moderation_log")
    op.drop_index("ix_moderation_log_actor_id", table_name="moderation_log")
    op.drop_table("moderation_log")

    op.drop_constraint("event_participants_user_id_fkey", "event_participants", type_="foreignkey")
    op.execute("DELETE FROM event_participants WHERE user_id IS NULL")
    op.alter_column("event_participants", "user_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "event_participants_user_id_fkey",
        "event_participants",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("hypotheses_author_id_fkey", "hypotheses", type_="foreignkey")
    op.execute("DELETE FROM hypotheses WHERE author_id IS NULL")
    op.alter_column("hypotheses", "author_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "hypotheses_author_id_fkey",
        "hypotheses",
        "users",
        ["author_id"],
        ["id"],
        ondelete="CASCADE",
    )
