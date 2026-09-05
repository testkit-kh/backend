"""Клиент STAC-каталога Sentinel-2 (Element84 Earth Search).

pystac-client использует requests (синхронный) — каждый поиск оборачивается
в asyncio.to_thread, иначе он стопорит весь event loop на время запроса.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pystac

from app.config import settings

logger = logging.getLogger(__name__)


class StacUnavailable(Exception):
    """STAC-каталог не настроен или не отвечает — ручка отвечает 503."""

    def __init__(self, detail: str, status_code: int = 503) -> None:
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def _search_sync(
    bbox: tuple[float, float, float, float],
    since: datetime | None,
    max_cloud_cover: float,
    limit: int,
) -> list[pystac.Item]:
    from pystac_client import Client

    client = Client.open(settings.STAC_API_URL)
    datetime_filter = f"{since.isoformat()}/.." if since else None
    search = client.search(
        collections=[settings.STAC_COLLECTION],
        bbox=list(bbox),
        datetime=datetime_filter,
        query={"eo:cloud_cover": {"lt": max_cloud_cover}},
        max_items=limit,
        limit=limit,
    )
    return list(search.items())


async def search_scenes(
    bbox: tuple[float, float, float, float],
    since: datetime | None = None,
    max_cloud_cover: float = 20.0,
    limit: int = 20,
) -> list[pystac.Item]:
    """Ищет сцены в STAC. Кидает StacUnavailable, если каталог не настроен/не отвечает."""
    if not settings.STAC_API_URL:
        raise StacUnavailable("STAC_API_URL не настроен.")

    try:
        return await asyncio.to_thread(_search_sync, bbox, since, max_cloud_cover, limit)
    except StacUnavailable:
        raise
    except Exception as exc:  # requests/pystac-client бросают разные типы ошибок
        logger.warning("STAC search failed: %s", exc)
        raise StacUnavailable(f"STAC-каталог сейчас недоступен: {exc}") from exc
