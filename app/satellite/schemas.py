"""Pydantic-схемы для /api/v1/satellite."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SceneRefreshRequest(BaseModel):
    bbox: tuple[float, float, float, float] = Field(
        description="min_lon, min_lat, max_lon, max_lat"
    )
    since: datetime | None = None
    max_cloud_cover: float = Field(default=20.0, ge=0, le=100)
    limit: int = Field(default=20, ge=1, le=100)
    organization_id: uuid.UUID | None = None


class SatelliteSceneOut(BaseModel):
    id: uuid.UUID
    stac_id: str
    collection: str
    datetime: datetime
    cloud_cover: float | None = None
    bbox: list[float]
    thumbnail_url: str | None = None
    organization_id: uuid.UUID | None = None
    #: Готовые шаблоны {z}/{x}/{y} для MapLibre raster source, через Caddy → titiler.
    tile_url_rgb: str
    tile_url_ndwi: str
    tile_url_ndti: str
    created_at: datetime


class SceneRefreshResponse(BaseModel):
    items: list[SatelliteSceneOut]


class SceneListResponse(BaseModel):
    items: list[SatelliteSceneOut]
    total: int


class DetectRequest(BaseModel):
    scene_id: uuid.UUID
    bbox: tuple[float, float, float, float] = Field(
        description="min_lon, min_lat, max_lon, max_lat — небольшой участок, не вся ООПТ"
    )
    index: Literal["ndwi", "ndti"]
    threshold: float | None = Field(default=None, description="Override дефолтного порога индекса")
    min_area_m2: float | None = Field(default=None, ge=0)


class DetectOut(BaseModel):
    scene_id: uuid.UUID
    index: str
    threshold_used: float
    polygons_found: int
    hypotheses_created: int
    hypothesis_ids: list[uuid.UUID]
