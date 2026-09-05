"""P1-5: курсор ленты «Мои точки» и колокольчик о вердикте."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.hypotheses import (
    _STATUS_NOTICE,
    decode_hypothesis_cursor,
    encode_hypothesis_cursor,
    notify_point_status_changed,
)
from app.models import HypothesisStatus, Notification, NotificationKind


def test_cursor_roundtrip():
    created_at = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    hypothesis_id = uuid.UUID("11111111-1111-1111-1111-111111111111")

    cursor = encode_hypothesis_cursor(created_at, hypothesis_id)

    assert "|" not in cursor
    assert decode_hypothesis_cursor(cursor) == (created_at, hypothesis_id)


def test_cursor_rejects_garbage():
    with pytest.raises(HTTPException) as caught:
        decode_hypothesis_cursor("not-a-cursor")
    assert caught.value.status_code == 400


def _register_volunteer(client):
    email = f"vol-{uuid.uuid4()}@example.com"
    registered = client.post(
        "/auth/register/volunteer",
        json={
            "email": email,
            "password": "supersecret123",
            "full_name": "Тестовый волонтёр",
            "is_over_14": True,
        },
    )
    assert registered.status_code == 201, registered.text
    login = client.post("/auth/login", data={"username": email, "password": "supersecret123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_my_hypotheses_empty_has_no_next_cursor(client):
    headers = _register_volunteer(client)
    response = client.get("/api/v1/hypotheses/my", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["next_cursor"] is None
    assert "offset" not in body


def test_my_hypotheses_rejects_broken_cursor(client):
    headers = _register_volunteer(client)
    response = client.get("/api/v1/hypotheses/my", headers=headers, params={"cursor": "%%%"})

    assert response.status_code == 400, response.text


def test_notify_point_status_uses_kind_and_reason():
    """Уведомление — in-app, не только строка в analytics_events."""
    added: list[Notification] = []

    class _Session:
        def add(self, obj):
            added.append(obj)

    author_id = uuid.uuid4()
    hypothesis_id = uuid.uuid4()
    notify_point_status_changed(
        _Session(),  # type: ignore[arg-type]
        author_id=author_id,
        hypothesis_id=hypothesis_id,
        new_status=HypothesisStatus.rejected,
        reject_reason="Точка вне территории ООПТ",
    )

    assert len(added) == 1
    notice = added[0]
    assert notice.kind == NotificationKind.point_validated
    assert notice.user_id == author_id
    assert notice.body == "Точка вне территории ООПТ"
    assert notice.payload["status"] == "rejected"
    assert notice.title == _STATUS_NOTICE[HypothesisStatus.rejected][0]
