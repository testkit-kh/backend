"""0015: выданные сертификаты «Чистого берега» (PLAN 5.6).

После approve координатором — запись с публичным кодом, PDF по запросу,
страница /verify/{code}.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-05
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "issued_certificates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("volunteer_id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=256), nullable=False),
        sa.Column("course", sa.String(length=256), nullable=False),
        sa.Column("points_confirmed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hours", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["volunteer_id"], ["volunteers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint("volunteer_id"),
    )
    op.create_index("ix_issued_certificates_code", "issued_certificates", ["code"])


def downgrade() -> None:
    op.drop_index("ix_issued_certificates_code", table_name="issued_certificates")
    op.drop_table("issued_certificates")
