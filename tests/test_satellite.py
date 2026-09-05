"""Sentinel-2: тайл-URL (регрессия на реальные баги titiler 2.x) + 503-контракт.

Живые STAC/titiler/S3 сюда намеренно не идут — тот же принцип, что и
staff_auth в test_monitoring_sites.py (verify_inn_external подменён,
чтобы тест не зависел от сети). Разбор titiler-эндпоинтов сделан вручную
(docker run titiler:2.2.1 + curl) при написании модуля; здесь закрепляем
находки как быстрый регрессионный тест на чистых функциях.
"""

import random
import uuid
from datetime import UTC, datetime

import pytest

from app.config import settings
from app.models import SatelliteScene
from app.satellite.service import _bbox_area_km2, build_tile_url

PASSWORD = "supersecret123"


@pytest.fixture
def staff_auth(client, monkeypatch):
    """Заголовок авторизации сотрудника — тот же паттерн, что и в других тестах."""

    async def _skip_registry(session, inn):
        return True, None

    monkeypatch.setattr("app.auth.verify_inn_external", _skip_registry)

    email = f"staff-{uuid.uuid4()}@example.com"
    registered = client.post(
        "/auth/register/organization",
        json={
            "org_name": f"Тестовая ООПТ {uuid.uuid4().hex[:6]}",
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


def _fake_scene() -> SatelliteScene:
    return SatelliteScene(
        id=uuid.uuid4(),
        stac_id="S2A_37TCK_20260903_0_L2A",
        collection="sentinel-2-l2a",
        datetime=datetime.now(UTC),
        cloud_cover=1.2,
        bbox=[37.0, 44.1, 37.9, 45.1],
        assets={
            "visual": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/x/TCI.tif",
            "green": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/x/B03.tif",
            "nir": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/x/B08.tif",
            "red": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/x/B04.tif",
        },
    )


def test_build_tile_url_uses_tile_matrix_set(monkeypatch):
    """titiler 2.x требует {tileMatrixSetId} в пути — без него 404 (проверено
    вживую). Регрессия на этот конкретный баг."""
    monkeypatch.setattr(settings, "TITILER_PUBLIC_URL", "https://example.test/titiler")
    monkeypatch.setattr(settings, "STAC_API_URL", "https://stac.example.test")
    monkeypatch.setattr(settings, "STAC_COLLECTION", "sentinel-2-l2a")

    url = build_tile_url(_fake_scene(), "rgb")

    assert "/cog/tiles/WebMercatorQuad/{z}/{x}/{y}" in url
    assert "url=" in url


def test_build_tile_url_ndwi_uses_positional_bands(monkeypatch):
    """titiler всегда именует полосы позиционно (b1, b2, ...) в порядке
    `assets=`, даже с asset_as_band=true — expression с именем ассета
    ("green") падает KeyError'ом внутри numexpr. Регрессия на этот баг:
    expression должен ссылаться на b1/b2, не на имена ассетов."""
    monkeypatch.setattr(settings, "TITILER_PUBLIC_URL", "https://example.test/titiler")
    monkeypatch.setattr(settings, "STAC_API_URL", "https://stac.example.test")
    monkeypatch.setattr(settings, "STAC_COLLECTION", "sentinel-2-l2a")

    url = build_tile_url(_fake_scene(), "ndwi")

    assert "/stac/tiles/WebMercatorQuad/{z}/{x}/{y}" in url
    assert "assets=green" in url
    assert "assets=nir" in url
    assert "asset_as_band" not in url
    assert "b1-b2" in url or "b1%2Db2" in url or "expression=" in url
    # Явно не должно быть имени ассета внутри expression= — только b1/b2.
    assert "green" not in url.split("expression=")[1].split("&")[0]


def test_bbox_area_km2_matches_known_extent():
    """~1°×1° у экватора ~111×111 км — грубая, но проверяемая оценка."""
    area = _bbox_area_km2((0.0, 0.0, 1.0, 1.0))
    assert 11000 < area < 13000


def test_scenes_refresh_503_when_unconfigured(client, staff_auth, monkeypatch):
    """Пустой STAC_API_URL/TITILER_PUBLIC_URL — 503, а не 500/крах API."""
    monkeypatch.setattr(settings, "STAC_API_URL", "")
    monkeypatch.setattr(settings, "TITILER_PUBLIC_URL", "")

    response = client.post(
        "/api/v1/satellite/scenes/refresh",
        headers=staff_auth,
        json={"bbox": [37.0, 44.0, 38.0, 45.0]},
    )

    assert response.status_code == 503, response.text


def test_scenes_nearest_503_when_unconfigured(client, staff_auth, monkeypatch):
    monkeypatch.setattr(settings, "STAC_API_URL", "")
    monkeypatch.setattr(settings, "TITILER_PUBLIC_URL", "")

    response = client.get(
        "/api/v1/satellite/scenes/nearest",
        headers=staff_auth,
        params={"lat": 44.6, "lon": 37.45},
    )

    assert response.status_code == 503, response.text


def test_detect_503_when_unconfigured(client, staff_auth, monkeypatch):
    monkeypatch.setattr(settings, "STAC_API_URL", "")
    monkeypatch.setattr(settings, "TITILER_PUBLIC_URL", "")

    response = client.post(
        "/api/v1/satellite/detect",
        headers=staff_auth,
        json={
            "scene_id": str(uuid.uuid4()),
            "bbox": [37.0, 44.0, 37.01, 44.01],
            "index": "ndwi",
        },
    )

    assert response.status_code == 503, response.text
