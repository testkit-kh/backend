"""0012: P1-4 приёмка «до/после» + retention считает третье событие.

events:
- photo_before_urls / photo_after_urls — принятая пара снимков
- before_after_accepted_at — момент приёмки; NULL значит ещё не принимали

kpi.retention_30d:
- в содержательные действия добавлен cleanup_event_before_after.
  joined и completed уже были в 0006; без приёмки третье событие
  таксономии по-прежнему некуда было писать.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-05
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_RETENTION_WITH_BEFORE_AFTER = """
-- Удержание: доля волонтёров, совершивших не меньше двух содержательных
-- действий за 30 дней после сертификата. Одно действие — это любопытство,
-- два — уже участие.
WITH certified AS (
    SELECT user_id, MIN(created_at) AS certified_at
    FROM analytics_events
    WHERE event_type = 'certificate_verified' AND payload ->> 'status' = 'approved'
    GROUP BY user_id
),
actions AS (
    SELECT e.user_id, COUNT(*) AS action_count
    FROM analytics_events e
    JOIN certified c ON c.user_id = e.user_id
    WHERE e.event_type IN (
        'point_created',
        'cleanup_event_joined',
        'cleanup_event_completed',
        'cleanup_event_before_after'
    )
      AND e.created_at BETWEEN c.certified_at AND c.certified_at + INTERVAL '30 days'
    GROUP BY e.user_id
)
SELECT
    DATE_TRUNC('week', c.certified_at)::date                        AS cohort_week,
    COUNT(*)                                                        AS certified,
    COUNT(*) FILTER (WHERE a.action_count >= 2)                     AS retained,
    ROUND(100.0 * COUNT(*) FILTER (WHERE a.action_count >= 2)
          / NULLIF(COUNT(*), 0), 1)                                 AS retention_pct
FROM certified c
LEFT JOIN actions a ON a.user_id = c.user_id
GROUP BY 1
ORDER BY 1
"""

_RETENTION_WITHOUT_BEFORE_AFTER = """
WITH certified AS (
    SELECT user_id, MIN(created_at) AS certified_at
    FROM analytics_events
    WHERE event_type = 'certificate_verified' AND payload ->> 'status' = 'approved'
    GROUP BY user_id
),
actions AS (
    SELECT e.user_id, COUNT(*) AS action_count
    FROM analytics_events e
    JOIN certified c ON c.user_id = e.user_id
    WHERE e.event_type IN ('point_created', 'cleanup_event_joined', 'cleanup_event_completed')
      AND e.created_at BETWEEN c.certified_at AND c.certified_at + INTERVAL '30 days'
    GROUP BY e.user_id
)
SELECT
    DATE_TRUNC('week', c.certified_at)::date                        AS cohort_week,
    COUNT(*)                                                        AS certified,
    COUNT(*) FILTER (WHERE a.action_count >= 2)                     AS retained,
    ROUND(100.0 * COUNT(*) FILTER (WHERE a.action_count >= 2)
          / NULLIF(COUNT(*), 0), 1)                                 AS retention_pct
FROM certified c
LEFT JOIN actions a ON a.user_id = c.user_id
GROUP BY 1
ORDER BY 1
"""


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("photo_before_urls", sa.ARRAY(sa.String(2048)), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("photo_after_urls", sa.ARRAY(sa.String(2048)), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("before_after_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(f"CREATE OR REPLACE VIEW kpi.retention_30d AS {_RETENTION_WITH_BEFORE_AFTER}")


def downgrade() -> None:
    op.execute(f"CREATE OR REPLACE VIEW kpi.retention_30d AS {_RETENTION_WITHOUT_BEFORE_AFTER}")
    op.drop_column("events", "before_after_accepted_at")
    op.drop_column("events", "photo_after_urls")
    op.drop_column("events", "photo_before_urls")
