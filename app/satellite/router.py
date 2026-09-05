"""Ручки Sentinel-2: сцены (STAC-поиск/апсерт) + автодетекция NDWI/NDTI."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_session
from app.hypotheses import _require_staff
from app.ml import _require_map_user
from app.models import User
from app.satellite import service
from app.satellite.schemas import (
    DetectOut,
    DetectRequest,
    SatelliteSceneOut,
    SceneListResponse,
    SceneRefreshRequest,
    SceneRefreshResponse,
)
from app.satellite.stac_client import StacUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/satellite", tags=["satellite"])


def _require_configured() -> None:
    if not settings.STAC_API_URL or not settings.TITILER_PUBLIC_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Спутниковый модуль не настроен (STAC_API_URL/TITILER_PUBLIC_URL).",
        )


@router.post("/scenes/refresh", response_model=SceneRefreshResponse)
async def refresh_scenes(
    body: SceneRefreshRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SceneRefreshResponse:
    _require_configured()
    _require_staff(user)

    try:
        scenes = await service.refresh_scenes(
            session,
            bbox=body.bbox,
            since=body.since,
            max_cloud_cover=body.max_cloud_cover,
            limit=body.limit,
            organization_id=body.organization_id,
        )
    except StacUnavailable as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return SceneRefreshResponse(items=[service.scene_to_out(s) for s in scenes])


@router.get("/scenes", response_model=SceneListResponse)
async def list_scenes(
    min_lon: float | None = Query(default=None),
    min_lat: float | None = Query(default=None),
    max_lon: float | None = Query(default=None),
    max_lat: float | None = Query(default=None),
    lat: float | None = Query(default=None),
    lon: float | None = Query(default=None),
    radius_m: float | None = Query(default=None, gt=0),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SceneListResponse:
    _require_configured()
    _require_map_user(user)

    bbox = None
    if None not in (min_lon, min_lat, max_lon, max_lat):
        bbox = (min_lon, min_lat, max_lon, max_lat)
    point = (lat, lon) if lat is not None and lon is not None else None

    scenes = await service.list_scenes(
        session, bbox=bbox, point=point, radius_m=radius_m, since=since, limit=limit
    )
    return SceneListResponse(items=[service.scene_to_out(s) for s in scenes], total=len(scenes))


@router.get("/scenes/nearest", response_model=SatelliteSceneOut)
async def nearest_scene(
    lat: float = Query(...),
    lon: float = Query(...),
    at: datetime | None = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SatelliteSceneOut:
    _require_configured()
    _require_map_user(user)

    scene = await service.find_nearest(session, lat=lat, lon=lon, at=at)
    if scene is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Нет сцены Sentinel-2 для этой точки — "
                "обновите сцены (refresh) для этого участка."
            ),
        )
    return service.scene_to_out(scene)


@router.get("/scenes/{scene_id}", response_model=SatelliteSceneOut)
async def get_scene(
    scene_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SatelliteSceneOut:
    _require_configured()
    _require_map_user(user)

    scene = await service.get_scene(session, scene_id)
    if scene is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сцена не найдена.")
    return service.scene_to_out(scene)


@router.post("/detect", response_model=DetectOut)
async def detect(
    body: DetectRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DetectOut:
    _require_configured()
    _require_staff(user)

    scene = await service.get_scene(session, body.scene_id)
    if scene is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сцена не найдена.")

    try:
        result = await service.detect(
            session,
            user=user,
            scene=scene,
            bbox=body.bbox,
            index=body.index,
            threshold=body.threshold,
            min_area_m2=body.min_area_m2,
        )
    except service.DetectAreaTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except service.DetectBandsMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return result
