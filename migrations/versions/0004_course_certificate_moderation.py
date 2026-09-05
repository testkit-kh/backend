"""Course/certificate moderation, coordinator role, notifications

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CERTIFICATE_STATUS = ("none", "pending", "approved", "rejected")
NOTIFICATION_KIND = (
    "course_not_started",
    "course_not_finished",
    "certificate_approved",
    "certificate_rejected",
    "point_validated",
    "cleanup_event_invite",
)

certificate_status = postgresql.ENUM(
    *CERTIFICATE_STATUS, name="certificate_status", create_type=False
)
notification_kind = postgresql.ENUM(
    *NOTIFICATION_KIND, name="notification_kind", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()

    # ---- Роль координатора -------------------------------------------------
    # ALTER TYPE ... ADD VALUE нельзя использовать в той же транзакции, где
    # значение добавлено, а Alembic держит всю миграцию в одной транзакции.
    # Поэтому пересоздаём тип целиком — детерминированно и без оговорок.
    op.execute("ALTER TYPE user_role RENAME TO user_role_old")
    postgresql.ENUM("volunteer", "staff", "coordinator", name="user_role").create(bind)
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE user_role "
        "USING role::text::user_role"
    )
    op.execute("DROP TYPE user_role_old")

    postgresql.ENUM(*CERTIFICATE_STATUS, name="certificate_status").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(*NOTIFICATION_KIND, name="notification_kind").create(
        bind, checkfirst=True
    )

    # ---- volunteers: обучение ---------------------------------------------
    op.alter_column("volunteers", "stepik_cert_url", new_column_name="certificate_url")
    op.add_column(
        "volunteers",
        sa.Column(
            "certificate_status",
            certificate_status,
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "volunteers",
        sa.Column("certificate_submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "volunteers",
        sa.Column("certificate_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "volunteers", sa.Column("certificate_reviewer_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "volunteers", sa.Column("certificate_reject_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "volunteers",
        sa.Column("course_redirect_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "volunteers",
        sa.Column("map_access_granted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_volunteers_certificate_reviewer_id",
        "volunteers",
        "users",
        ["certificate_reviewer_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Раньше загрузка сертификата сразу ставила is_trained = true без проверки.
    # Приводим уже существующие записи в согласованное состояние, чтобы у
    # обученных волонтёров не оказалось статуса "none".
    op.execute(
        "UPDATE volunteers SET certificate_status = 'approved' WHERE is_trained = true"
    )
    op.execute(
        "UPDATE volunteers SET certificate_status = 'pending' "
        "WHERE is_trained = false AND certificate_url IS NOT NULL"
    )

    # Очередь модерации: выбираем pending по дате подачи.
    op.create_index(
        "ix_volunteers_certificate_status",
        "volunteers",
        ["certificate_status", "certificate_submitted_at"],
    )

    # ---- notifications -----------------------------------------------------
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", notification_kind, nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("action_url", sa.String(2048), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index(
        "ix_notifications_user_created", "notifications", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_notifications_unread",
        "notifications",
        ["user_id"],
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_unread", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_volunteers_certificate_status", table_name="volunteers")
    op.drop_constraint(
        "fk_volunteers_certificate_reviewer_id", "volunteers", type_="foreignkey"
    )
    for column in (
        "map_access_granted_at",
        "course_redirect_at",
        "certificate_reject_reason",
        "certificate_reviewer_id",
        "certificate_reviewed_at",
        "certificate_submitted_at",
        "certificate_status",
    ):
        op.drop_column("volunteers", column)
    op.alter_column("volunteers", "certificate_url", new_column_name="stepik_cert_url")

    bind = op.get_bind()
    postgresql.ENUM(name="notification_kind").drop(bind, checkfirst=True)
    postgresql.ENUM(name="certificate_status").drop(bind, checkfirst=True)

    # Обратно к двум ролям. Координаторов, если они появились, понижаем до
    # staff — иначе приведение типа упадёт на неизвестном значении.
    op.execute("UPDATE users SET role = 'staff' WHERE role = 'coordinator'")
    op.execute("ALTER TYPE user_role RENAME TO user_role_old")
    postgresql.ENUM("volunteer", "staff", name="user_role").create(bind)
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE user_role "
        "USING role::text::user_role"
    )
    op.execute("DROP TYPE user_role_old")
