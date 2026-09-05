"""Список площадок наблюдений — регрессия на 500.

GET /api/v1/monitoring-sites отдавал 500 любому сотруднику. Причина —
MonitoringSite.organization с lazy="joined": список считает замеры агрегатом с
GROUP BY monitoring_sites.id, а неявный LEFT JOIN подмешивал в выборку колонки
organizations, которых в GROUP BY нет. PostgreSQL такой запрос отвергает.
"""

import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest

PASSWORD = "supersecret123"


@pytest.fixture
def staff_auth(client, monkeypatch):
    """Заголовок авторизации сотрудника вместе с его ООПТ.

    verify_inn_external подменён: регистрация организации иначе ходит в ЕГРЮЛ,
    а тест не должен зависеть ни от сети, ни от доступности ФНС.
    """

    async def _skip_registry(session, inn):
        return True, None

    monkeypatch.setattr("app.auth.verify_inn_external", _skip_registry)

    email = f"staff-{uuid.uuid4()}@example.com"
    registered = client.post(
        "/auth/register/organization",
        json={
            "org_name": f"Тестовая ООПТ {uuid.uuid4().hex[:6]}",
            # Контрольная сумма не важна: проверку ИНН подменили выше.
            "inn": "".join(str(random.randint(0, 9)) for _ in range(10)),
            "email": email,
            "password": PASSWORD,
            "full_name": "Тестовый сотрудник",
        },
    )
    assert registered.status_code == 201, registered.text

    login = client.post("/auth/login", data={"username": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_empty_site_list_does_not_fail(client, staff_auth):
    """Пустой список падал так же, как непустой: PostgreSQL отвергал запрос
    при разборе, до того как дело доходило до строк."""
    response = client.get("/api/v1/monitoring-sites", headers=staff_auth)

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_site_list_returns_survey_counters(client, staff_auth):
    """Тот же агрегат, но уже с данными: счётчики обязаны считаться одним
    запросом, а не обходом relationship по каждой площадке."""
    now = datetime.now(UTC)
    code = f"TST-{uuid.uuid4().hex[:6].upper()}"

    created = client.post(
        "/api/v1/monitoring-sites",
        headers=staff_auth,
        json={
            "name": "Тестовый пляж",
            "code": code,
            "established_at": (now - timedelta(days=400)).isoformat(),
            "shoreline_length_m": 100.0,
        },
    )
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]

    for days_ago in (30, 10):
        survey = client.post(
            f"/api/v1/monitoring-sites/{site_id}/surveys",
            headers=staff_auth,
            json={
                "surveyed_at": (now - timedelta(days=days_ago)).isoformat(),
                "item_count": 120,
                "trash": {"dominant_category": "plastic", "estimated_volume_m3": 1.5},
            },
        )
        assert survey.status_code == 201, survey.text

    listed = client.get("/api/v1/monitoring-sites", headers=staff_auth)

    assert listed.status_code == 200, listed.text
    rows = listed.json()
    # Ровно одна: сотрудник видит только площадки своей ООПТ, а она новая.
    assert [row["code"] for row in rows] == [code]
    assert rows[0]["surveys_count"] == 2
    assert rows[0]["last_surveyed_at"] is not None
