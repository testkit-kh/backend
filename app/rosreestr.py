"""
Обёртка над `rosreestr2coord` (https://github.com/rendrom/rosreestr2coord) —
открытая библиотека, ключей не требует.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)

#: Кадастровый номер: КК:РР:ЧЧЧЧЧЧЧ:УУ. Тот же формат, что проверяет фронт
#: (`src/lib/registry.ts`) — обе проверки обязаны совпадать.
CADASTRAL_RE = re.compile(r"^\d{2}:\d{2}:\d{6,7}:\d{1,10}$")

#: Сколько ждать ФГИС, прежде чем признать попытку неудачной.
RESOLVE_TIMEOUT_SECONDS = 90
#: Таймаут одного HTTP-запроса внутри библиотеки.
HTTP_TIMEOUT_SECONDS = 30


def is_valid_cadastral_number(raw: str) -> bool:
    return bool(CADASTRAL_RE.match(raw.strip()))


@dataclass(frozen=True)
class ParcelGeometry:
    """GeoJSON-геометрия участка, уже приведённая к MultiPolygon."""

    geojson: dict
    source: str = "rosreestr"


class ParcelNotFound(RuntimeError):
    """Росреестр ответил, но участка с таким номером нет."""


class ParcelResolveFailed(RuntimeError):
    """Росреестр не ответил или отдал что-то неразбираемое."""


def _to_multipolygon(geometry: dict) -> dict:
    """Приводит Polygon к MultiPolygon.

    В БД колонка объявлена MULTIPOLYGON: многоконтурные участки — норма, и
    хранить два разных типа в одной колонке значит потом ветвиться в каждом
    запросе.
    """
    if geometry.get("type") == "Polygon":
        return {"type": "MultiPolygon", "coordinates": [geometry["coordinates"]]}
    return geometry


def _resolve_blocking(cadastral_number: str) -> dict:
    """Синхронный вызов библиотеки. Выполняется в отдельном потоке."""
    # Импорт внутри функции: пакет тянет тяжёлые зависимости и сетевые
    # обёртки, а нужен только в фоновой задаче.
    from rosreestr2coord.parser import Area

    area = Area(
        code=cadastral_number,
        coord_out="EPSG:4326",
        with_log=False,
        use_cache=True,
        # По умолчанию библиотека ждёт 5 секунд — для ФГИС ЕГРН этого не
        # хватает почти никогда. Свой таймаут снаружи всё равно жёстче.
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    feature_collection = area.to_geojson_poly()
    if not feature_collection:
        raise ParcelNotFound(cadastral_number)

    import json

    parsed = (
        json.loads(feature_collection)
        if isinstance(feature_collection, str)
        else feature_collection
    )
    features = parsed.get("features") or []
    if not features:
        raise ParcelNotFound(cadastral_number)

    geometry = features[0].get("geometry")
    if not geometry or not geometry.get("coordinates"):
        # Участок в ЕГРН есть, но без координат — так бывает у ранее
        # учтённых. Для нас это «границ нет», а не «участка нет».
        raise ParcelNotFound(cadastral_number)

    return _to_multipolygon(geometry)


async def resolve_parcel(cadastral_number: str) -> ParcelGeometry:
    """Границы участка. Бросает ParcelNotFound / ParcelResolveFailed."""
    if not settings.ROSREESTR_ENABLED:
        # Там, где до ФГИС не достучаться, бессмысленно держать пользователя
        # и воркер по полторы минуты на каждом участке: сразу говорим, что
        # границы вводятся вручную.
        raise ParcelResolveFailed(
            "Автоматический резолвинг отключён (ROSREESTR_ENABLED=false): "
            "задайте границы вручную через PUT /api/v1/parcels/{id}/geometry"
        )

    if not is_valid_cadastral_number(cadastral_number):
        raise ParcelResolveFailed(
            f"Неверный формат кадастрового номера: {cadastral_number}"
        )

    try:
        # Библиотека синхронная и ходит в сеть — в event loop её пускать
        # нельзя, иначе один медленный участок блокирует весь сервис.
        geometry = await asyncio.wait_for(
            asyncio.to_thread(_resolve_blocking, cadastral_number),
            timeout=RESOLVE_TIMEOUT_SECONDS,
        )
    except ParcelNotFound:
        raise
    except TimeoutError as error:
        raise ParcelResolveFailed(
            f"ФГИС ЕГРН не ответил за {RESOLVE_TIMEOUT_SECONDS} с"
        ) from error
    except Exception as error:
        logger.warning("Не удалось получить участок %s: %s", cadastral_number, error)
        raise ParcelResolveFailed(str(error)) from error

    return ParcelGeometry(geojson=geometry)
