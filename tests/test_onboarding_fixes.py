"""Анкета, территория без кадастра, согласие в профиле, presign, координатор."""

import uuid
from datetime import date

from app.config import settings


def _volunteer_headers(client, *, birth_date: date | None = None):
    email = f"vol-{uuid.uuid4()}@example.com"
    payload = {
        "email": email,
        "password": "supersecret123",
        "full_name": "Волонтёр теста",
        "is_over_14": True,
    }
    if birth_date is not None:
        payload["birth_date"] = birth_date.isoformat()
        del payload["is_over_14"]
    registered = client.post("/auth/register/volunteer", json=payload)
    assert registered.status_code == 201, registered.text
    login = client.post("/auth/login", data={"username": email, "password": "supersecret123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, registered.json()


def test_education_upsert_is_idempotent(client, monkeypatch):
    async def _no_registry(session, inn, **kwargs):
        return None

    monkeypatch.setattr("app.volunteers.lookup_company", _no_registry)
    headers, _ = _volunteer_headers(client)

    first = client.post(
        "/api/v1/volunteers/me/education",
        headers=headers,
        json={
            "level": "school",
            "institution_name": "МБОУ Гимназия № 39",
            "institution_inn": "7707083893",
            "grade": "9",
            "city": "Петропавловск-Камчатский",
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["level"] == "school"
    assert first.json()["grade"] == "9"

    second = client.post(
        "/api/v1/volunteers/me/education",
        headers=headers,
        json={"level": "college", "city": "Петропавловск-Камчатский"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["level"] == "college"
    assert second.json()["grade"] is None

    listed = client.get("/api/v1/volunteers/me/education", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["level"] == "college"


def test_latest_consent_is_null_until_submitted(client):
    """awaiting после регистрации ≠ документ подан."""
    headers, profile = _volunteer_headers(client, birth_date=date(2010, 1, 1))
    assert profile["consent_status"] == "awaiting"
    assert profile["latest_consent"] is None

    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["consent_status"] == "awaiting"
    assert me.json()["latest_consent"] is None

    submitted = client.post(
        "/api/v1/volunteers/me/parental-consent",
        headers=headers,
        json={
            "representative_name": "Иванова Мария",
            "representative_phone": "+79001234567",
            "representative_email": "parent@example.com",
            "relation": "мать",
        },
    )
    assert submitted.status_code == 201, submitted.text

    me_after = client.get("/auth/me", headers=headers)
    assert me_after.json()["latest_consent"] is not None
    assert me_after.json()["latest_consent"]["representative_name"] == "Иванова Мария"


def test_territory_patch_stores_osm_source(client, monkeypatch):
    async def _skip_registry(session, inn):
        return True, None

    monkeypatch.setattr("app.auth.verify_inn_external", _skip_registry)
    email = f"staff-{uuid.uuid4()}@example.com"
    registered = client.post(
        "/auth/register/organization",
        json={
            "org_name": "Кроноцкий",
            "inn": "".join(str((i * 7 + 3) % 10) for i in range(10)),
            "email": email,
            "password": "supersecret123",
            "full_name": "Сотрудник",
        },
    )
    assert registered.status_code == 201, registered.text
    login = client.post("/auth/login", data={"username": email, "password": "supersecret123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    patched = client.patch(
        "/api/v1/organizations/me/territory",
        headers=headers,
        json={
            "source": "osm",
            "osm_id": "relation/2800189",
            "name": "Кроноцкий заповедник",
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [[[[160.0, 54.0], [160.1, 54.0], [160.1, 54.1], [160.0, 54.0]]]],
            },
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["source"] == "osm"
    assert patched.json()["osm_id"] == "relation/2800189"
    assert patched.json()["has_territory"] is True

    profile = client.get("/api/v1/organizations/me", headers=headers)
    assert profile.status_code == 200, profile.text
    assert profile.json()["territory_source"] == "osm"
    assert profile.json()["territory_osm_id"] == "relation/2800189"
    assert profile.json()["has_territory"] is True


def test_presign_rejects_unknown_type_and_issues_url(client):
    headers, _ = _volunteer_headers(client)
    bad = client.post(
        "/api/v1/uploads/presign",
        headers=headers,
        json={"filename": "x.exe", "content_type": "application/x-msdownload"},
    )
    assert bad.status_code == 415

    ok = client.post(
        "/api/v1/uploads/presign",
        headers=headers,
        json={
            "filename": "photo.jpg",
            "content_type": "image/jpeg",
            "purpose": "hypothesis_photo",
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["method"] == "PUT"
    assert body["upload_url"].startswith("http")
    assert body["public_url"].endswith(".jpg")
    assert "hypothesis_photo/" in body["key"]


def test_coordinator_register_requires_configured_code(client, monkeypatch):
    monkeypatch.setattr(settings, "COORDINATOR_INVITE_CODE", "")
    off = client.post(
        "/auth/register/coordinator",
        json={
            "invite_code": "whatever1",
            "email": f"c-{uuid.uuid4()}@example.com",
            "password": "supersecret123",
            "full_name": "Координатор",
        },
    )
    assert off.status_code == 503

    monkeypatch.setattr(settings, "COORDINATOR_INVITE_CODE", "COORD-SECRET-CODE")
    bad = client.post(
        "/auth/register/coordinator",
        json={
            "invite_code": "wrong-code-xx",
            "email": f"c-{uuid.uuid4()}@example.com",
            "password": "supersecret123",
            "full_name": "Координатор",
        },
    )
    assert bad.status_code == 403

    email = f"c-{uuid.uuid4()}@example.com"
    ok = client.post(
        "/auth/register/coordinator",
        json={
            "invite_code": "COORD-SECRET-CODE",
            "email": email,
            "password": "supersecret123",
            "full_name": "Координатор",
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["role"] == "coordinator"


def test_my_hypotheses_accepts_limit_above_hundred(client):
    headers, _ = _volunteer_headers(client)
    response = client.get("/api/v1/hypotheses/my", headers=headers, params={"limit": 200})
    assert response.status_code == 200, response.text
    assert response.json()["limit"] == 200
