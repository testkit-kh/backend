#!/usr/bin/env python
"""
Провижининг Metabase: подключение к БД, карточки и дашборды — скриптом.

Зачем скрипт, а не «настроить руками один раз»: в OSS-версии Metabase нет
экспорта/импорта конфигурации (serialization — это Enterprise). Без скрипта
знание о том, как собран дашборд, живёт только внутри чужого контейнера, и
поднять проект заново нельзя.

Идемпотентен: повторный запуск обновляет существующие карточки, а не плодит
дубли. Сверка по имени — у Metabase нет понятия «внешний ключ» для карточек.

Запуск:
    python scripts/metabase_seed.py --url http://localhost:3000 \\
        --email admin@example.ru --password ... \\
        --db-host db --db-name eco_project

Версия API проверялась на Metabase v0.50+. Если ручки разъедутся — смотреть
ответ /api/session и /api/dashboard в конкретной сборке.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# Что именно собираем
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    name: str
    sql: str
    display: str = "table"
    description: str = ""
    #: Параметры дашборда, которые карточка принимает. Для запертых дашбордов
    #: это `organization_id` — он приходит из подписанного токена.
    template_tags: dict[str, Any] = field(default_factory=dict)


ORG_TAG = {
    "organization_id": {
        "id": "org-id-tag",
        "name": "organization_id",
        "display-name": "Организация",
        "type": "text",
        # Не required: координатор смотрит тот же дашборд по всей программе.
        "required": False,
    }
}


FUNNEL_CARDS = [
    Card(
        name="Воронка по каналам",
        description="Регистрация → курс → возврат → сертификат → первая точка",
        display="table",
        sql="SELECT * FROM kpi.funnel_by_source ORDER BY registered DESC",
    ),
    Card(
        name="Возврат из курса по неделям",
        description="Ключевая метрика риска: доля вернувшихся со Школы",
        display="line",
        sql="SELECT cohort_week, return_rate_pct FROM kpi.return_rate ORDER BY cohort_week",
    ),
    Card(
        name="Проверка сертификатов",
        description="Сколько подано, принято, отклонено и сколько ждут очереди",
        display="bar",
        sql="SELECT week, approved, rejected, awaiting_review FROM kpi.course_completion"
        " ORDER BY week",
    ),
    Card(
        name="Активация после обучения",
        display="line",
        sql="SELECT cohort_week, activation_rate_pct FROM kpi.activation ORDER BY cohort_week",
    ),
    Card(
        name="Удержание за 30 дней",
        display="line",
        sql="SELECT cohort_week, retention_pct FROM kpi.retention_30d ORDER BY cohort_week",
    ),
    Card(
        name="Эффективность напоминаний",
        display="table",
        sql="SELECT * FROM kpi.reminder_effectiveness",
    ),
]

OOPT_CARDS = [
    Card(
        name="Очередь и вердикты по территориям",
        display="table",
        template_tags=ORG_TAG,
        sql="SELECT organization_name, points_received, points_validated, queue_size,"
        " approved, rejected, drone_requested, is_active"
        " FROM kpi.oopt_engagement"
        " WHERE {{organization_id}} IS NULL"
        "    OR organization_id::text = {{organization_id}}"
        " ORDER BY queue_size DESC",
    ),
    Card(
        name="Время до вердикта",
        description="Медиана рядом со средним: одна забытая точка перекашивает среднее",
        display="table",
        template_tags=ORG_TAG,
        sql="SELECT week, validated, avg_hours, median_hours, max_hours"
        " FROM kpi.validation_time"
        " WHERE {{organization_id}} IS NULL"
        "    OR organization_id::text = {{organization_id}}"
        " ORDER BY week DESC",
    ),
    Card(
        name="Всплески точек (антифрод)",
        description="Не блокирует, а показывает список для проверки глазами",
        display="table",
        sql="SELECT * FROM kpi.antifraud_bursts LIMIT 100",
    ),
]

IMPACT_CARDS = [
    Card(
        name="Найдено мусора по составу",
        display="bar",
        template_tags=ORG_TAG,
        sql="SELECT dominant_category, SUM(volume_m3) AS volume_m3, SUM(mass_kg) AS mass_kg"
        " FROM kpi.trash_found"
        " WHERE {{organization_id}} IS NULL"
        "    OR organization_id::text = {{organization_id}}"
        " GROUP BY 1 ORDER BY 2 DESC NULLS LAST",
    ),
    Card(
        name="Прогноз затрат на уборку",
        description="По проектным допущениям, не фактическая смета",
        display="bar",
        template_tags=ORG_TAG,
        sql="SELECT access_type, SUM(cleanup_cost_rub) AS cost_rub, SUM(points) AS points"
        " FROM kpi.trash_found"
        " WHERE {{organization_id}} IS NULL"
        "    OR organization_id::text = {{organization_id}}"
        " GROUP BY 1 ORDER BY 2 DESC NULLS LAST",
    ),
    Card(
        name="Точки без оценки объёма",
        description="Их нельзя учесть в смете — видно, сколько данных недобирается",
        display="scalar",
        sql="SELECT SUM(points_without_estimate) AS points_without_estimate FROM kpi.trash_found",
    ),
    Card(
        name="Активность по дням",
        display="line",
        sql="SELECT day, SUM(events) AS events FROM kpi.daily_activity GROUP BY 1 ORDER BY 1",
    ),
]

DASHBOARDS = [
    ("Воронка и обучение", FUNNEL_CARDS, "METABASE_DASHBOARD_FUNNEL"),
    ("Операционка территории", OOPT_CARDS, "METABASE_DASHBOARD_OOPT"),
    ("Экологический эффект", IMPACT_CARDS, "METABASE_DASHBOARD_IMPACT"),
]


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------


class Metabase:
    def __init__(self, base_url: str, email: str, password: str) -> None:
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(timeout=TIMEOUT)
        token = self._post("/api/session", {"username": email, "password": password})["id"]
        self.client.headers["X-Metabase-Session"] = token

    def _post(self, path: str, body: dict) -> dict:
        response = self.client.post(f"{self.base}{path}", json=body)
        response.raise_for_status()
        return response.json()

    def _put(self, path: str, body: dict) -> dict:
        response = self.client.put(f"{self.base}{path}", json=body)
        response.raise_for_status()
        return response.json()

    def _get(self, path: str) -> Any:
        response = self.client.get(f"{self.base}{path}")
        response.raise_for_status()
        return response.json()

    # -- база --------------------------------------------------------------
    def ensure_database(self, name: str, conn: dict) -> int:
        for database in self._get("/api/database")["data"]:
            if database["name"] == name:
                return database["id"]
        return self._post(
            "/api/database",
            {"name": name, "engine": "postgres", "details": conn, "is_full_sync": True},
        )["id"]

    # -- карточки ----------------------------------------------------------
    def ensure_card(self, card: Card, database_id: int) -> int:
        body = {
            "name": card.name,
            "description": card.description or None,
            "display": card.display,
            "visualization_settings": {},
            "dataset_query": {
                "type": "native",
                "database": database_id,
                "native": {"query": card.sql, "template-tags": card.template_tags},
            },
        }
        for existing in self._get("/api/card"):
            if existing["name"] == card.name:
                # Обновляем, а не создаём: иначе каждый прогон плодит копии.
                return self._put(f"/api/card/{existing['id']}", body)["id"]
        return self._post("/api/card", body)["id"]

    # -- дашборды ----------------------------------------------------------
    def ensure_dashboard(self, name: str, cards: list[Card], card_ids: list[int], scoped: bool) -> int:
        dashboard_id = None
        for existing in self._get("/api/dashboard"):
            if existing["name"] == name:
                dashboard_id = existing["id"]
                break
        if dashboard_id is None:
            dashboard_id = self._post("/api/dashboard", {"name": name})["id"]

        parameters = (
            [
                {
                    "id": "org-id-tag",
                    "name": "organization_id",
                    "slug": "organization_id",
                    "type": "category",
                }
            ]
            if scoped
            else []
        )

        # Раскладка в две колонки: у Metabase сетка 24 единицы в ширину.
        # Маппинг параметра — по конкретной карточке, а не по дашборду целиком:
        # в одном дашборде могут соседствовать карточки с {{organization_id}}
        # и без него (например, антифрод-список общий на всех), и Metabase
        # отвергает весь embed, если параметр смаплен на карточку, в чьём
        # запросе такого тега нет ("Unknown parameter :organization_id").
        dashcards = []
        for index, (card, card_id) in enumerate(zip(cards, card_ids)):
            has_org_param = bool(card.template_tags)
            dashcards.append(
                {
                    "id": -(index + 1),
                    "card_id": card_id,
                    "row": (index // 2) * 6,
                    "col": (index % 2) * 12,
                    "size_x": 12,
                    "size_y": 6,
                    "parameter_mappings": (
                        [
                            {
                                "parameter_id": "org-id-tag",
                                "card_id": card_id,
                                "target": ["variable", ["template-tag", "organization_id"]],
                            }
                        ]
                        if has_org_param
                        else []
                    ),
                }
            )

        self._put(
            f"/api/dashboard/{dashboard_id}",
            {"parameters": parameters, "dashcards": dashcards, "enable_embedding": True},
        )
        return dashboard_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--db-host", default="db")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--db-name", default="eco_project")
    parser.add_argument(
        "--db-user",
        default="metabase_ro",
        help="Роль только для чтения: у неё доступ лишь к схеме kpi",
    )
    parser.add_argument("--db-password", default="metabase_ro")
    args = parser.parse_args()

    metabase = Metabase(args.url, args.email, args.password)
    database_id = metabase.ensure_database(
        "Чистый берег — KPI",
        {
            "host": args.db_host,
            "port": args.db_port,
            "dbname": args.db_name,
            "user": args.db_user,
            "password": args.db_password,
            "schema-filters-type": "inclusion",
            "schema-filters-patterns": "kpi",
        },
    )
    print(f"База подключена: id={database_id}")

    env_lines = []
    for name, cards, env_var in DASHBOARDS:
        scoped = any(card.template_tags for card in cards)
        card_ids = [metabase.ensure_card(card, database_id) for card in cards]
        dashboard_id = metabase.ensure_dashboard(name, cards, card_ids, scoped)
        print(f"  {name}: дашборд {dashboard_id}, карточек {len(card_ids)}")
        env_lines.append(f"{env_var}={dashboard_id}")

    print("\nДобавьте в .env бэкенда:")
    print("\n".join(env_lines))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.HTTPStatusError as error:
        print(
            f"Metabase ответил {error.response.status_code}: {error.response.text[:400]}",
            file=sys.stderr,
        )
        sys.exit(1)
    except httpx.HTTPError as error:
        print(f"Metabase недоступен: {error}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print("Metabase вернул не JSON — проверьте --url", file=sys.stderr)
        sys.exit(1)
