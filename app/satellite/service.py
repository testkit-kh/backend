"""Апсерт сцен, поиск ближайшей/по bbox, тайл-URL, спектральная автодетекция.

Два независимых пути вычислений:
- тайлы (визуализация) собираются здесь только как URL-шаблон — сами
  пиксели браузер тянет напрямую из titiler, бэкенд их не видит;
- /detect — единственное место, где бэкенд сам читает пиксели COG, и
  только для небольшого bbox (см. _bbox_area_km2 / SATELLITE_DETECT_MAX_AREA_KM2).
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import pystac
from geoalchemy2 import Geography
from geoalchemy2.functions import ST_DWithin
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import EventType, emit
from app.config import settings
from app.hypotheses import _find_org_with_buffer
from app.models import Hypothesis, HypothesisSource, HypothesisStatus, SatelliteScene, User
from app.satellite import stac_client
from app.satellite.schemas import DetectOut, SatelliteSceneOut

IndexName = Literal["ndwi", "ndti"]

# Обе пары — родные 10-метровые бэнды Sentinel-2 (red/green/blue/nir), поэтому
# передискретизация между ними не нужна.
_INDEX_BANDS: dict[str, tuple[str, str]] = {
    "ndwi": ("green", "nir"),
    "ndti": ("red", "green"),
}
# titiler's expression evaluator always names bands positionally as b1, b2, ...
# in the order the `assets=` params were given — checked against a live
# titiler 2.2.1: `asset_as_band=true` only renames band *metadata*, the
# expression itself still KeyErrors on the literal asset name ("green").
# So b1/b2 below refer to _INDEX_BANDS[mode] in order, not the band's own name.
_INDEX_EXPRESSION: dict[str, str] = {
    "ndwi": "(b1-b2)/(b1+b2)",  # (green-nir)/(green+nir) — same sign as _run_detection_sync
    "ndti": "(b1-b2)/(b1+b2)",  # (red-green)/(red+green)
}
#: Хакатонные дефолты, не откалиброванные под конкретный берег — см. риски в плане.
_DEFAULT_THRESHOLD: dict[str, float] = {"ndwi": 0.2, "ndti": 0.1}
_DEFAULT_MIN_AREA_M2 = 900.0


class DetectAreaTooLarge(Exception):
    """bbox запроса больше SATELLITE_DETECT_MAX_AREA_KM2 — 422, не 500."""


class DetectBandsMissing(Exception):
    """В сцене нет нужных для индекса ассетов — 422."""


# ── upsert / чтение сцен ─────────────────────────────────────────────────────


async def refresh_scenes(
    session: AsyncSession,
    *,
    bbox: tuple[float, float, float, float],
    since: datetime | None,
    max_cloud_cover: float,
    limit: int,
    organization_id: uuid.UUID | None,
) -> list[SatelliteScene]:
    items = await stac_client.search_scenes(
        bbox, since=since, max_cloud_cover=max_cloud_cover, limit=limit
    )
    scenes = [await _upsert_scene(session, item, organization_id) for item in items]
    await session.flush()
    return scenes


async def _upsert_scene(
    session: AsyncSession,
    item: pystac.Item,
    organization_id: uuid.UUID | None,
) -> SatelliteScene:
    assets = {name: asset.href for name, asset in item.assets.items() if asset.href}
    cloud_cover = item.properties.get("eo:cloud_cover")
    geom_value = None
    if item.geometry:
        geom_value = func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(item.geometry)), 4326)

    existing = await session.scalar(
        select(SatelliteScene).where(SatelliteScene.stac_id == item.id)
    )
    if existing is not None:
        existing.cloud_cover = cloud_cover
        if item.bbox:
            existing.bbox = list(item.bbox)
        if geom_value is not None:
            existing.geom = geom_value
        existing.assets = assets
        existing.thumbnail_url = assets.get("thumbnail") or existing.thumbnail_url
        if organization_id is not None:
            existing.organization_id = organization_id
        return existing

    scene = SatelliteScene(
        id=uuid.uuid4(),
        stac_id=item.id,
        collection=item.collection_id or settings.STAC_COLLECTION,
        datetime=item.datetime or datetime.now(UTC),
        cloud_cover=cloud_cover,
        bbox=list(item.bbox) if item.bbox else [],
        geom=geom_value,
        assets=assets,
        thumbnail_url=assets.get("thumbnail"),
        organization_id=organization_id,
    )
    session.add(scene)
    await session.flush()
    return scene


async def get_scene(session: AsyncSession, scene_id: uuid.UUID) -> SatelliteScene | None:
    return await session.scalar(select(SatelliteScene).where(SatelliteScene.id == scene_id))


async def find_nearest(
    session: AsyncSession, *, lat: float, lon: float, at: datetime | None = None
) -> SatelliteScene | None:
    """Ближайшая по времени сцена, чей footprint покрывает точку.

    Нет сцены — возвращаем None (пусто в UI), а не сцену из другого региона:
    ложное «берег чистый» хуже честного пустого состояния.
    """
    at = at or datetime.now(UTC)
    point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
    q = (
        select(SatelliteScene)
        .where(
            SatelliteScene.geom.isnot(None),
            func.ST_Intersects(SatelliteScene.geom, point),
        )
        .order_by(func.abs(func.extract("epoch", SatelliteScene.datetime - at)))
        .limit(1)
    )
    return await session.scalar(q)


async def list_scenes(
    session: AsyncSession,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    point: tuple[float, float] | None = None,
    radius_m: float | None = None,
    since: datetime | None = None,
    limit: int = 50,
) -> list[SatelliteScene]:
    q = select(SatelliteScene)

    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        envelope = func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
        q = q.where(
            SatelliteScene.geom.isnot(None), func.ST_Intersects(SatelliteScene.geom, envelope)
        )

    if point is not None and radius_m is not None:
        lat, lon = point
        geog_point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326).cast(Geography(srid=4326))
        geog_geom = SatelliteScene.geom.cast(Geography(srid=4326))
        q = q.where(SatelliteScene.geom.isnot(None), ST_DWithin(geog_geom, geog_point, radius_m))

    if since is not None:
        q = q.where(SatelliteScene.datetime >= since)

    q = q.order_by(SatelliteScene.datetime.desc()).limit(limit)
    result = await session.execute(q)
    return list(result.scalars().all())


# ── тайл-URL (titiler) ───────────────────────────────────────────────────────


def _item_url(stac_id: str) -> str:
    return f"{settings.STAC_API_URL}/collections/{settings.STAC_COLLECTION}/items/{stac_id}"


#: titiler 2.x требует TileMatrixSet в пути (/cog/tiles/{tileMatrixSetId}/{z}/{x}/{y}) —
#: проверено вживую (без него 404). WebMercatorQuad — то, что понимает MapLibre.
_TMS = "WebMercatorQuad"


def build_tile_url(scene: SatelliteScene, mode: Literal["rgb", "ndwi", "ndti"]) -> str:
    base = settings.TITILER_PUBLIC_URL.rstrip("/")

    if mode == "rgb":
        # "visual" — готовый 3-канальный true-color COG, есть у каждой
        # сцены Earth Search sentinel-2-l2a.
        visual = scene.assets.get("visual")
        return f"{base}/cog/tiles/{_TMS}/{{z}}/{{x}}/{{y}}?url={quote(visual or '', safe='')}"

    band_a, band_b = _INDEX_BANDS[mode]
    expression = _INDEX_EXPRESSION[mode]
    item_url = quote(_item_url(scene.stac_id), safe="")
    # Реальный NDWI/NDTI почти никогда не доходит до ±1 (типично −0.3…0.5) —
    # rescale=-1,1 без цветокарты давал тусклый, почти однотонный серый тайл
    # (проверено вживую). rdbu уже даёт нужную семантику: вода (плюс) — синим,
    # суша (минус) — красным, без отдельной логики цвета на фронте.
    return (
        f"{base}/stac/tiles/{_TMS}/{{z}}/{{x}}/{{y}}"
        f"?url={item_url}"
        f"&assets={band_a}&assets={band_b}"
        f"&expression={quote(expression, safe='')}"
        f"&rescale=-0.3,0.5"
        f"&colormap_name=rdbu"
    )


def scene_to_out(scene: SatelliteScene) -> SatelliteSceneOut:
    return SatelliteSceneOut(
        id=scene.id,
        stac_id=scene.stac_id,
        collection=scene.collection,
        datetime=scene.datetime,
        cloud_cover=scene.cloud_cover,
        bbox=list(scene.bbox),
        thumbnail_url=scene.thumbnail_url,
        organization_id=scene.organization_id,
        tile_url_rgb=build_tile_url(scene, "rgb"),
        tile_url_ndwi=build_tile_url(scene, "ndwi"),
        tile_url_ndti=build_tile_url(scene, "ndti"),
        created_at=scene.created_at,
    )


# ── детекция (NDWI/NDTI) ─────────────────────────────────────────────────────


def _bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    """Равнопромежуточная оценка на широте центроида — достаточно для лимита."""
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = (min_lat + max_lat) / 2
    lat_km = (max_lat - min_lat) * 111.32
    lon_km = (max_lon - min_lon) * 111.32 * math.cos(math.radians(mid_lat))
    return abs(lat_km * lon_km)


def _run_detection_sync(
    scene: SatelliteScene,
    bbox: tuple[float, float, float, float],
    index: str,
    threshold: float,
    min_area_m2: float,
) -> list[dict[str, Any]]:
    """Блокирующий rasterio/scipy пайплайн — вызывать только через asyncio.to_thread."""
    import numpy as np
    import rasterio
    from rasterio.features import shapes as rio_shapes
    from rasterio.warp import transform_bounds, transform_geom
    from scipy import ndimage
    from shapely.geometry import shape as shapely_shape

    band_a_name, band_b_name = _INDEX_BANDS[index]
    href_a = scene.assets.get(band_a_name)
    href_b = scene.assets.get(band_b_name)
    if not href_a or not href_b:
        raise DetectBandsMissing(
            f"В сцене {scene.stac_id} нет ассетов «{band_a_name}»/«{band_b_name}»."
        )

    with rasterio.open(href_a) as src_a, rasterio.open(href_b) as src_b:
        # Критично: bbox запроса приходит в EPSG:4326, COG — в родной UTM.
        # Окно, вырезанное в lon/lat напрямую по UTM-растру, тихо вернёт
        # пустой/мусорный массив — поэтому bbox сначала перепроецируется.
        native_bbox = transform_bounds("EPSG:4326", src_a.crs, *bbox)
        window_a = src_a.window(*native_bbox)
        window_b = src_b.window(*native_bbox)
        a = src_a.read(1, window=window_a, boundless=True, fill_value=0).astype("float32")
        b = src_b.read(1, window=window_b, boundless=True, fill_value=0).astype("float32")
        transform = src_a.window_transform(window_a)
        pixel_w, pixel_h = src_a.res
        crs = src_a.crs

    denom = a + b
    index_arr = np.divide(a - b, denom, out=np.zeros_like(a, dtype="float32"), where=denom != 0)
    mask = index_arr > threshold
    mask = ndimage.binary_opening(mask)
    mask = ndimage.binary_closing(mask)
    labeled, n_labels = ndimage.label(mask)

    pixel_area_m2 = abs(pixel_w * pixel_h)
    min_area_px = max(1, int(min_area_m2 / pixel_area_m2))
    # NDWI прежде всего отделяет воду от суши: на любом прибрежном bbox всё
    # море целиком проходит порог как один гигантский компонент — проверено
    # вживую (полигон на пол-Чёрного моря вместо локальной аномалии).
    # Компонент шире четверти окна — это водоём целиком, а не находка.
    max_area_px = int(0.25 * a.size)

    results: list[dict[str, Any]] = []
    for label_id in range(1, n_labels + 1):
        component = labeled == label_id
        count = int(component.sum())
        if count < min_area_px or count > max_area_px:
            continue
        area_m2 = count * pixel_area_m2
        component_shapes = rio_shapes(
            component.astype("uint8"), mask=component, transform=transform
        )
        for geom, value in component_shapes:
            if value != 1:
                continue
            geom_wgs84 = transform_geom(crs, "EPSG:4326", geom)
            centroid = shapely_shape(geom_wgs84).centroid
            results.append(
                {"geometry": geom_wgs84, "area_m2": area_m2, "lat": centroid.y, "lon": centroid.x}
            )
    return results


async def detect(
    session: AsyncSession,
    *,
    user: User,
    scene: SatelliteScene,
    bbox: tuple[float, float, float, float],
    index: IndexName,
    threshold: float | None,
    min_area_m2: float | None,
) -> DetectOut:
    area_km2 = _bbox_area_km2(bbox)
    if area_km2 > settings.SATELLITE_DETECT_MAX_AREA_KM2:
        raise DetectAreaTooLarge(
            f"Площадь запроса {area_km2:.1f} км² превышает лимит "
            f"{settings.SATELLITE_DETECT_MAX_AREA_KM2:.1f} км² — выберите участок поменьше."
        )

    threshold_used = threshold if threshold is not None else _DEFAULT_THRESHOLD[index]
    min_area = min_area_m2 if min_area_m2 is not None else _DEFAULT_MIN_AREA_M2

    polygons = await asyncio.to_thread(
        _run_detection_sync, scene, bbox, index, threshold_used, min_area
    )

    hypothesis_ids: list[uuid.UUID] = []
    for poly in polygons:
        point = func.ST_SetSRID(func.ST_MakePoint(poly["lon"], poly["lat"]), 4326)
        cand_org = await _find_org_with_buffer(session, point)
        geom_value = func.ST_SetSRID(
            func.ST_GeomFromGeoJSON(json.dumps(poly["geometry"])), 4326
        )

        hypothesis = Hypothesis(
            id=uuid.uuid4(),
            author_id=user.id,
            organization_id=cand_org,
            lat=poly["lat"],
            lon=poly["lon"],
            location=point,
            geom=geom_value,
            description=(
                f"Автодетекция (satellite_auto): аномалия {index.upper()} на снимке "
                f"Sentinel-2 от {scene.datetime:%d.%m.%Y}, площадь ~{poly['area_m2']:.0f} м². "
                "Требует подтверждения сотрудником ООПТ."
            ),
            status=HypothesisStatus.pending,
            source=HypothesisSource.satellite_auto,
            estimated_area_m2=poly["area_m2"],
            # trash_categories/dominant_category/fraction — None: спектральный
            # индекс не говорит о составе мусора, в отличие от uav_auto.
        )
        session.add(hypothesis)
        await session.flush()
        hypothesis_ids.append(hypothesis.id)

        await emit(
            session,
            EventType.point_created,
            user_id=user.id,
            lat=poly["lat"],
            lon=poly["lon"],
            payload={
                "hypothesis_id": str(hypothesis.id),
                "organization_id": str(cand_org) if cand_org else None,
                "mode": "satellite_auto",
                "scene_id": str(scene.id),
                "index": index,
                "area_m2": poly["area_m2"],
            },
        )
        if cand_org is not None:
            await emit(
                session,
                EventType.point_received_in_zone,
                user_id=user.id,
                lat=poly["lat"],
                lon=poly["lon"],
                payload={
                    "hypothesis_id": str(hypothesis.id),
                    "organization_id": str(cand_org),
                    "mode": "satellite_auto",
                    "scene_id": str(scene.id),
                },
            )

    await session.flush()

    return DetectOut(
        scene_id=scene.id,
        index=index,
        threshold_used=threshold_used,
        polygons_found=len(polygons),
        hypotheses_created=len(hypothesis_ids),
        hypothesis_ids=hypothesis_ids,
    )
