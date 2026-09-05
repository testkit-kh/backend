"""Открытые ручки: публичная карта и виджеты для сайтов ООПТ.

Авторизации нет намеренно: лендинг и встраиваемый виджет не имеют JWT.
Отсюда жёсткое правило к составу свойств — только то, что не является
персональными данными.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from geoalchemy2.functions import ST_AsGeoJSON
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Hypothesis, HypothesisStatus
from app.schemas import GeoJSONFeature, GeoJSONFeatureCollection, GeoJSONGeometry, GeoJSONProperties

router = APIRouter(prefix="/api/v1/public", tags=["public"])


@router.get(
    "/points.geojson",
    response_model=GeoJSONFeatureCollection,
    summary="Обезличенные подтверждённые точки (без авторизации)",
)
async def public_points_geojson(
    session: AsyncSession = Depends(get_session),
):
    """Только approved: pending — не проверенные, rejected — отказ,
    cleaned — уже убранные и живут в другой витрине.

    В свойствах нет author_id, ФИО, почты, фото. Описание точки — про мусор,
    не про человека; его оставляем, иначе виджету нечего показать.
    """
    query = select(
        Hypothesis.id,
        Hypothesis.lat,
        Hypothesis.lon,
        Hypothesis.description,
        Hypothesis.status,
        ST_AsGeoJSON(Hypothesis.geom).label("geojson"),
    ).where(Hypothesis.status == HypothesisStatus.approved)

    result = await session.execute(query)
    features: list[GeoJSONFeature] = []
    for row in result.all():
        if row.geojson:
            geom = json.loads(row.geojson)
            geometry = GeoJSONGeometry(type=geom["type"], coordinates=geom["coordinates"])
        else:
            geometry = GeoJSONGeometry(type="Point", coordinates=[row.lon, row.lat])

        features.append(
            GeoJSONFeature(
                geometry=geometry,
                properties=GeoJSONProperties(
                    id=row.id,
                    description=row.description,
                    status=row.status.value,
                    layer="public_approved",
                ),
            )
        )

    payload = GeoJSONFeatureCollection(features=features)
    return JSONResponse(
        content=payload.model_dump(mode="json"),
        media_type="application/geo+json",
    )
