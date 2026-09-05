"""KPI views in a dedicated `kpi` schema + read-only grants for Metabase

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-05

Витрины живут в миграции, а не создаются руками в psql: формула каждого KPI
из паспорта проекта должна быть версионируемой и воспроизводимой — на защите
карточка Metabase открывается, и виден ровно этот SQL.

Отдельная схема `kpi` — граница безопасности. Metabase подключается ролью
`metabase_ro`, у которой есть доступ только сюда. Сырые `users`,
`analytics_events` и `parental_consents` в BI не попадают: в дашборд не должны
утекать персональные данные, тем более несовершеннолетних.

Обычные вью, а не материализованные: объёмы событий на пилоте небольшие, а
матвьюхи требуют расписания обновления и показывают вчерашние цифры там, где
ООПТ ждёт сегодняшние. Переводить в материализованные — когда упрётся.
"""

from __future__ import annotations

from typing import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Окно возврата с курса: сколько дней ждём человека обратно, прежде чем
# считать, что он не вернулся. Допущение, а не измеренная величина.
RETURN_WINDOW_DAYS = 30

VIEWS: dict[str, str] = {}


# ═══════════════════════════════════════════════════════════════════════════
# Воронка волонтёра
# ═══════════════════════════════════════════════════════════════════════════

VIEWS["funnel_by_source"] = """
-- Воронка целиком, в разрезе канала привлечения.
-- Каждый шаг считается по уникальным пользователям, а не по событиям: один
-- человек может кликнуть «на курс» пять раз, но в воронке он один.
WITH registered AS (
    SELECT
        user_id,
        COALESCE(payload ->> 'source', 'direct') AS source,
        MIN(created_at)                          AS registered_at
    FROM analytics_events
    WHERE event_type = 'user_registered'
    GROUP BY user_id, payload ->> 'source'
),
step AS (
    SELECT event_type, user_id, MIN(created_at) AS at
    FROM analytics_events
    WHERE event_type IN (
        'course_redirect_click',
        'app_reopened_post_redirect',
        'certificate_verified',
        'point_created'
    )
      AND (event_type <> 'certificate_verified'
           OR payload ->> 'status' = 'approved')
    GROUP BY event_type, user_id
)
SELECT
    r.source,
    COUNT(*)                                                       AS registered,
    COUNT(rc.at)                                                   AS clicked_course,
    COUNT(ret.at)                                                  AS returned,
    COUNT(cert.at)                                                 AS certified,
    COUNT(pt.at)                                                   AS activated,
    ROUND(100.0 * COUNT(rc.at)   / NULLIF(COUNT(*), 0), 1)         AS pct_to_course,
    ROUND(100.0 * COUNT(cert.at) / NULLIF(COUNT(rc.at), 0), 1)     AS pct_completed_course,
    ROUND(100.0 * COUNT(pt.at)   / NULLIF(COUNT(cert.at), 0), 1)   AS pct_activated
FROM registered r
LEFT JOIN step rc   ON rc.user_id   = r.user_id AND rc.event_type   = 'course_redirect_click'
LEFT JOIN step ret  ON ret.user_id  = r.user_id AND ret.event_type  = 'app_reopened_post_redirect'
LEFT JOIN step cert ON cert.user_id = r.user_id AND cert.event_type = 'certificate_verified'
LEFT JOIN step pt   ON pt.user_id   = r.user_id AND pt.event_type   = 'point_created'
GROUP BY r.source
"""


VIEWS["return_rate"] = f"""
-- Ключевая метрика риска: доля вернувшихся из «слепой зоны» курса.
-- Между уходом на внешнюю платформу и возвращением мы человека не видим,
-- поэтому возврат фиксируется явным событием, а не догадкой.
WITH redirects AS (
    SELECT user_id, MIN(created_at) AS redirected_at
    FROM analytics_events
    WHERE event_type = 'course_redirect_click'
    GROUP BY user_id
),
returns AS (
    SELECT user_id, MIN(created_at) AS returned_at
    FROM analytics_events
    WHERE event_type IN ('app_reopened_post_redirect', 'certificate_uploaded')
    GROUP BY user_id
)
SELECT
    DATE_TRUNC('week', r.redirected_at)::date AS cohort_week,
    COUNT(*)                                  AS redirected,
    COUNT(*) FILTER (
        WHERE ret.returned_at IS NOT NULL
          AND ret.returned_at <= r.redirected_at + INTERVAL '{RETURN_WINDOW_DAYS} days'
    )                                         AS returned_in_window,
    ROUND(100.0 * COUNT(*) FILTER (
        WHERE ret.returned_at IS NOT NULL
          AND ret.returned_at <= r.redirected_at + INTERVAL '{RETURN_WINDOW_DAYS} days'
    ) / NULLIF(COUNT(*), 0), 1)               AS return_rate_pct,
    {RETURN_WINDOW_DAYS}                      AS window_days
FROM redirects r
LEFT JOIN returns ret ON ret.user_id = r.user_id AND ret.returned_at > r.redirected_at
GROUP BY 1
ORDER BY 1
"""


VIEWS["reminder_effectiveness"] = """
-- Отдельно органический возврат и возврат после напоминания. Без этой
-- развязки нельзя ответить, работают ли напоминания вообще: те, кто вернулся
-- бы сам, иначе засчитываются напоминанию.
WITH sent AS (
    SELECT
        user_id,
        payload ->> 'variant' AS variant,
        id                    AS event_id,
        created_at            AS sent_at
    FROM analytics_events
    WHERE event_type = 'reminder_sent'
),
clicked AS (
    SELECT user_id, MIN(created_at) AS clicked_at
    FROM analytics_events
    WHERE event_type = 'reminder_clicked'
    GROUP BY user_id
),
returned AS (
    SELECT user_id, MIN(created_at) AS returned_at
    FROM analytics_events
    WHERE event_type IN ('app_reopened_post_redirect', 'certificate_uploaded')
    GROUP BY user_id
)
SELECT
    COALESCE(s.variant, 'default')                                  AS variant,
    COUNT(DISTINCT s.user_id)                                       AS reminded_users,
    COUNT(DISTINCT c.user_id)                                       AS clicked_users,
    COUNT(DISTINCT r.user_id) FILTER (WHERE c.clicked_at IS NOT NULL
                                        AND r.returned_at >= c.clicked_at)
                                                                    AS returned_after_click,
    ROUND(100.0 * COUNT(DISTINCT c.user_id)
          / NULLIF(COUNT(DISTINCT s.user_id), 0), 1)                AS click_rate_pct
FROM sent s
LEFT JOIN clicked c  ON c.user_id = s.user_id
LEFT JOIN returned r ON r.user_id = s.user_id
GROUP BY 1
"""


VIEWS["course_completion"] = """
-- Доля дошедших до подтверждённого сертификата и время, которое сертификат
-- лежит в очереди на проверку: это узкое место воронки, пока он не проверен,
-- волонтёр не может поставить ни одной точки.
WITH uploads AS (
    SELECT user_id, MIN(created_at) AS uploaded_at
    FROM analytics_events
    WHERE event_type = 'certificate_uploaded'
    GROUP BY user_id
),
verdicts AS (
    SELECT
        user_id,
        payload ->> 'status'                         AS status,
        payload ->> 'method'                         AS method,
        (payload ->> 'time_to_review')::numeric      AS time_to_review,
        created_at                                   AS reviewed_at
    FROM analytics_events
    WHERE event_type = 'certificate_verified'
)
SELECT
    DATE_TRUNC('week', u.uploaded_at)::date                        AS week,
    COUNT(*)                                                       AS uploaded,
    COUNT(*) FILTER (WHERE v.status = 'approved')                  AS approved,
    COUNT(*) FILTER (WHERE v.status = 'rejected')                  AS rejected,
    COUNT(*) FILTER (WHERE v.status IS NULL)                       AS awaiting_review,
    ROUND(100.0 * COUNT(*) FILTER (WHERE v.status = 'approved')
          / NULLIF(COUNT(*), 0), 1)                                AS approval_rate_pct,
    ROUND(AVG(v.time_to_review) / 3600.0, 1)                       AS avg_review_hours
FROM uploads u
LEFT JOIN verdicts v ON v.user_id = u.user_id
GROUP BY 1
ORDER BY 1
"""


VIEWS["activation"] = """
-- Активация: сколько выпускников курса поставили хотя бы одну точку и как
-- быстро. Долгий разрыв между сертификатом и первой точкой означает, что
-- человека нечем занять сразу после обучения.
WITH certified AS (
    SELECT user_id, MIN(created_at) AS certified_at
    FROM analytics_events
    WHERE event_type = 'certificate_verified' AND payload ->> 'status' = 'approved'
    GROUP BY user_id
),
first_point AS (
    SELECT user_id, MIN(created_at) AS first_point_at
    FROM analytics_events
    WHERE event_type = 'point_created'
    GROUP BY user_id
)
SELECT
    DATE_TRUNC('week', c.certified_at)::date                       AS cohort_week,
    COUNT(*)                                                       AS certified,
    COUNT(p.first_point_at)                                        AS activated,
    ROUND(100.0 * COUNT(p.first_point_at) / NULLIF(COUNT(*), 0), 1) AS activation_rate_pct,
    ROUND(AVG(EXTRACT(EPOCH FROM (p.first_point_at - c.certified_at)) / 86400.0)::numeric, 1)
                                                                   AS avg_days_to_first_point
FROM certified c
LEFT JOIN first_point p ON p.user_id = c.user_id AND p.first_point_at >= c.certified_at
GROUP BY 1
ORDER BY 1
"""


VIEWS["retention_30d"] = """
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


# ═══════════════════════════════════════════════════════════════════════════
# ООПТ
# ═══════════════════════════════════════════════════════════════════════════

VIEWS["oopt_engagement"] = """
-- Вовлечённость ООПТ. Организация считается активной, если хоть раз вынесла
-- вердикт: зарегистрироваться и не смотреть предложку — это не участие.
-- `organization_id` берётся из payload события, а не join'ится с гипотезами:
-- витрина не должна ломаться, если гипотезу удалили.
WITH received AS (
    SELECT
        (payload ->> 'organization_id')::uuid AS organization_id,
        COUNT(*)                              AS points_received,
        MAX(created_at)                       AS last_point_at
    FROM analytics_events
    WHERE event_type = 'point_received_in_zone'
      AND payload ->> 'organization_id' IS NOT NULL
    GROUP BY 1
),
validated AS (
    SELECT
        (payload ->> 'organization_id')::uuid                           AS organization_id,
        COUNT(*)                                                        AS points_validated,
        COUNT(*) FILTER (WHERE payload ->> 'status' = 'approved')       AS approved,
        COUNT(*) FILTER (WHERE payload ->> 'status' = 'rejected')       AS rejected,
        COUNT(*) FILTER (WHERE payload ->> 'status' = 'drone_requested') AS drone_requested,
        MAX(created_at)                                                 AS last_validation_at
    FROM analytics_events
    WHERE event_type = 'point_validated'
      AND payload ->> 'organization_id' IS NOT NULL
    GROUP BY 1
)
SELECT
    o.id                                                AS organization_id,
    o.name                                              AS organization_name,
    o.verification_status::text                         AS verification_status,
    COALESCE(r.points_received, 0)                      AS points_received,
    COALESCE(v.points_validated, 0)                     AS points_validated,
    COALESCE(v.approved, 0)                             AS approved,
    COALESCE(v.rejected, 0)                             AS rejected,
    COALESCE(v.drone_requested, 0)                      AS drone_requested,
    COALESCE(r.points_received, 0) - COALESCE(v.points_validated, 0) AS queue_size,
    (v.points_validated > 0)                            AS is_active,
    r.last_point_at,
    v.last_validation_at
FROM organizations o
LEFT JOIN received  r ON r.organization_id = o.id
LEFT JOIN validated v ON v.organization_id = o.id
"""


VIEWS["validation_time"] = """
-- Операционная метрика ООПТ: сколько точка ждёт вердикта. Медиана рядом со
-- средним намеренно — одна забытая на месяц точка перекашивает среднее, и по
-- нему одному нельзя понять, есть ли проблема.
SELECT
    (payload ->> 'organization_id')::uuid                                AS organization_id,
    DATE_TRUNC('week', created_at)::date                                 AS week,
    COUNT(*)                                                             AS validated,
    ROUND(AVG((payload ->> 'time_to_validate')::numeric) / 3600.0, 1)    AS avg_hours,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
        ORDER BY (payload ->> 'time_to_validate')::numeric
    )::numeric / 3600.0, 1)                                              AS median_hours,
    ROUND(MAX((payload ->> 'time_to_validate')::numeric) / 3600.0, 1)    AS max_hours
FROM analytics_events
WHERE event_type = 'point_validated'
  AND payload ->> 'time_to_validate' IS NOT NULL
GROUP BY 1, 2
ORDER BY 2
"""


# ═══════════════════════════════════════════════════════════════════════════
# Экологический эффект
# ═══════════════════════════════════════════════════════════════════════════

VIEWS["trash_found"] = """
-- Сколько мусора найдено и во что обойдётся уборка. Считается только по
-- точкам, где человек указал состав и объём: остальные попадают в
-- points_without_estimate, а не в нули. Смета с дырами лучше, чем смета,
-- которая молча занижает.
SELECT
    (e.payload ->> 'organization_id')::uuid                        AS organization_id,
    e.payload ->> 'dominant_category'                              AS dominant_category,
    e.payload ->> 'access_type'                                    AS access_type,
    DATE_TRUNC('month', e.created_at)::date                        AS month,
    COUNT(*)                                                       AS points,
    COUNT(*) FILTER (WHERE e.payload ->> 'volume_m3' IS NULL)      AS points_without_estimate,
    ROUND(SUM((e.payload ->> 'volume_m3')::numeric), 1)            AS volume_m3,
    ROUND(SUM((e.payload ->> 'mass_kg')::numeric), 1)              AS mass_kg,
    ROUND(SUM((e.payload ->> 'cleanup_cost_rub')::numeric), 0)     AS cleanup_cost_rub
FROM analytics_events e
WHERE e.event_type = 'point_created'
GROUP BY 1, 2, 3, 4
ORDER BY 4
"""


VIEWS["daily_activity"] = """
-- Общий ряд активности: что происходит в системе по дням. Нужен и как
-- «пульс» на дашборде, и чтобы заметить провал раньше, чем о нём спросят.
SELECT
    DATE_TRUNC('day', created_at)::date AS day,
    event_type,
    COUNT(*)                            AS events,
    COUNT(DISTINCT user_id)             AS users
FROM analytics_events
GROUP BY 1, 2
ORDER BY 1 DESC, 3 DESC
"""


# ═══════════════════════════════════════════════════════════════════════════
# Антифрод
# ═══════════════════════════════════════════════════════════════════════════

VIEWS["antifraud_bursts"] = """
-- Всплески: много точек от одного человека за короткое окно. Само по себе
-- это не мошенничество — на выезде так и работают, — поэтому витрина ничего
-- не блокирует, а показывает список для глазами.
SELECT
    user_id,
    DATE_TRUNC('hour', created_at)                        AS hour,
    COUNT(*)                                              AS points,
    COUNT(*) FILTER (WHERE payload ->> 'has_photo' = 'false') AS without_photo,
    MIN(created_at)                                       AS first_at,
    MAX(created_at)                                       AS last_at
FROM analytics_events
WHERE event_type = 'point_created'
GROUP BY 1, 2
HAVING COUNT(*) >= 10
ORDER BY 3 DESC
"""


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS kpi")

    for name, body in VIEWS.items():
        op.execute(f"CREATE OR REPLACE VIEW kpi.{name} AS {body}")

    # Роль Metabase создаётся при инициализации кластера (зона devops). Если
    # её нет — миграция не должна падать: на CI и на локальной машине BI не
    # поднимают. Поэтому гранты выдаются условно.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'metabase_ro') THEN
                GRANT USAGE ON SCHEMA kpi TO metabase_ro;
                GRANT SELECT ON ALL TABLES IN SCHEMA kpi TO metabase_ro;
                ALTER DEFAULT PRIVILEGES IN SCHEMA kpi
                    GRANT SELECT ON TABLES TO metabase_ro;
                -- Явно отбираем доступ к сырым данным: вью читают их от имени
                -- владельца, а сама роль не должна видеть ни персональных
                -- данных, ни согласий несовершеннолетних.
                REVOKE ALL ON ALL TABLES IN SCHEMA public FROM metabase_ro;
                REVOKE USAGE ON SCHEMA public FROM metabase_ro;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    for name in VIEWS:
        op.execute(f"DROP VIEW IF EXISTS kpi.{name}")
    op.execute("DROP SCHEMA IF EXISTS kpi CASCADE")
