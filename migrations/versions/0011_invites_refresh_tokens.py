"""0011: P1-6 инвайты сотрудников + P1-7 refresh-токены.

Новые таблицы:
- staff_invites  — одноразовые коды регистрации сотрудников ООПТ
- refresh_tokens — выданные refresh-токены; хранится только SHA-256, сам
  токен в базу не попадает

Колонки профиля организации (contact_email / contact_phone / description)
здесь не заводятся — они уже созданы ревизией 0010.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-05
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- staff_invites -----------------------------------------------------
    op.create_table(
        "staff_invites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("used_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        # SET NULL, а не CASCADE: выдавший или использовавший код сотрудник
        # может быть удалён, а запись о выдаче доступа должна остаться.
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["used_by_id"], ["users.id"], ondelete="SET NULL"),
    )
    # Уникальность кода — не удобство, а требование: одинаковые коды сделали
    # бы неопределённым то, в какую ООПТ впускать.
    op.create_index("ix_staff_invites_code", "staff_invites", ["code"], unique=True)
    op.create_index("ix_staff_invites_organization_id", "staff_invites", ["organization_id"])
    op.create_index("ix_staff_invites_org_used", "staff_invites", ["organization_id", "used_at"])

    # ---- refresh_tokens ----------------------------------------------------
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # 64 символа — ровно SHA-256 в hex.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    # Частичный индекс под массовый отзыв при обнаружении кражи: нужны только
    # активные токены пользователя, а отозванных со временем становится
    # больше, и в этот индекс они не попадают.
    op.create_index(
        "ix_refresh_tokens_user_active",
        "refresh_tokens",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_user_active", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_token_hash", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    op.drop_index("ix_staff_invites_org_used", table_name="staff_invites")
    op.drop_index("ix_staff_invites_organization_id", table_name="staff_invites")
    op.drop_index("ix_staff_invites_code", table_name="staff_invites")
    op.drop_table("staff_invites")
