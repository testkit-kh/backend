"""P1-4: приёмка «до/после» — валидация тела запроса."""

import pytest
from pydantic import ValidationError

from app.analytics.events import EventType
from app.schemas import EventBeforeAfterRequest


def test_after_photos_are_required():
    with pytest.raises(ValidationError):
        EventBeforeAfterRequest(photo_after_urls=[])


def test_before_may_be_empty_and_filled_from_hypothesis_later():
    body = EventBeforeAfterRequest(photo_after_urls=["https://cdn.example/after.jpg"])
    assert body.photo_before_urls == []
    assert body.photo_after_urls == ["https://cdn.example/after.jpg"]


def test_blank_urls_are_stripped():
    body = EventBeforeAfterRequest(
        photo_before_urls=["  ", "https://cdn.example/before.jpg"],
        photo_after_urls=["https://cdn.example/after.jpg", ""],
    )
    assert body.photo_before_urls == ["https://cdn.example/before.jpg"]
    assert body.photo_after_urls == ["https://cdn.example/after.jpg"]


def test_taxonomy_has_before_after_event():
    assert EventType.cleanup_event_before_after.value == "cleanup_event_before_after"
