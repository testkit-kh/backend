"""0010: контактные поля организации для кабинета ООПТ.

Новые колонки в organizations:
- contact_email, contact_phone, description — редактируемые сотрудником
  через PATCH /api/v1/organizations/me.

Название и ИНН редактированию не подлежат — они канонические, взяты из
ЕГРЮЛ при регистрации, поэтому отдельных колонок для них не заводим.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-05
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("contact_email", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("contact_phone", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "description")
    op.drop_column("organizations", "contact_phone")
    op.drop_column("organizations", "contact_email")
