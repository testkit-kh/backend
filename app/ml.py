"""
ML-сканы: прокси к ml.{DOMAIN}, сохранение находок и кандидатов в очередь ООПТ.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from geoalchemy2.functions import ST_MakePoint, ST_SetSRID
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app import ml_client
from app.age import has_field_access
from app.analytics import EventType, emit
from app.auth import get_current_user
from app.cleanup_cost import TrashCategory, TrashFraction
from app.database import get_session
from app.hypotheses import _find_org_with_buffer
from app.ml_client import MlUnavailable
from app.models import (
    CertificateStatus,
    Hypothesis,
    HypothesisSource,
    HypothesisStatus,
    MlFinding,
    MlScan,
    User,
    UserRole,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ml", tags=["ml"])


# ── request / response schemas ──────────────────────────────────────────────


class MlScanCreateRequest(BaseModel):
    bbox: tuple[float, float, float, float] = Field(
        description="min_lon, min_lat, max_lon, max_lat"
    )
    zoom: int = Field(default=18, ge=1, le=23)
    source: str | None = Field(default=None, description="Имя тайлового источника ML")
    territory_id: int | None = None
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    min_area_px: int | None = Field(default=None, ge=1)


class MlFindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scan_id: uuid.UUID
    detection_id: int | None = None
    lat: float | None = None
    lon: float | None = None
    trash_categories: list[str] | None = None
    dominant_category: str | None = None
    fraction: str | None = None
    confidence: float | None = None
    estimated_volume_m3: float | None = None
    estimated_mass_kg: float | None = None
    label_ru: str | None = None
    color_hex: str | None = None
    hypothesis_id: uuid.UUID | None = None
    created_at: datetime


class MlScanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requester_id: uuid.UUID | None = None
    organization_id: uuid.UUID | None = None
    bbox: list[float]
    zoom: int
    tile_source: str | None = None
    ml_job_id: str | None = None
    summary: dict[str, Any] | None = None
    geojson: dict[str, Any] | None = None
    overlay_bounds: list | None = None
    imagery: dict[str, Any] | None = None
    fraud_flags: list | None = None
    model_info: dict[str, Any] | None = None
    candidates_suppressed: bool
    findings_count: int = 0
    hypotheses_created: int = 0
    created_at: datetime
    findings: list[MlFindingOut] | None = None


class MlScanListOut(BaseModel):
    items: list[MlScanOut]
    total: int


class MlFindingListOut(BaseModel):
    items: list[MlFindingOut]
    total: int


class MlHealthOut(BaseModel):
    configured: bool
    status: str
    detail: str | None = None
    backend: str | None = None
    backend_ready: bool | None = None
    trained: bool | None = None
    version: str | None = None


# ── access ──────────────────────────────────────────────────────────────────


def _require_map_user(user: User) -> None:
    """Staff, coordinator или обученный волонтёр с доступом к карте."""
    if user.role == UserRole.coordinator:
        return
    if user.role == UserRole.staff and user.staff is not None:
        return
    if user.role == UserRole.volunteer and user.volunteer is not None:
        vol = user.volunteer
        if not has_field_access(vol):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Нужно согласие законного представителя.",
            )
        if vol.certificate_status != CertificateStatus.approved:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Карта и ML-сканы открываются после проверки сертификата.",
            )
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Недостаточно прав для ML-сканов.",
    )


def _org_id_for_user(user: User) -> uuid.UUID | None:
    if user.role == UserRole.staff and user.staff is not None:
        return user.staff.organization_id
    return None


def _raise_ml(exc: MlUnavailable) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _parse_trash_category(raw: str | None) -> TrashCategory | None:
    if not raw:
        return None
    try:
        return TrashCategory(raw)
    except ValueError:
        return None


def _parse_fraction(raw: str | None) -> TrashFraction | None:
    if not raw:
        return None
    try:
        return TrashFraction(raw)
    except ValueError:
        return None


def _detection_lookup(detections: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {
        int(d["id"]): d
        for d in detections
        if isinstance(d, dict) and "id" in d
    }


def _scan_out(
    scan: MlScan,
    *,
    include_findings: bool = False,
    findings: list[MlFinding] | None = None,
) -> MlScanOut:
    rows = findings if findings is not None else list(scan.findings or [])
    hyp_count = sum(1 for f in rows if f.hypothesis_id is not None)
    return MlScanOut(
        id=scan.id,
        requester_id=scan.requester_id,
        organization_id=scan.organization_id,
        bbox=list(scan.bbox) if scan.bbox else [],
        zoom=scan.zoom,
        tile_source=scan.tile_source,
        ml_job_id=scan.ml_job_id,
        summary=scan.summary,
        geojson=scan.geojson,
        overlay_bounds=scan.overlay_bounds,
        imagery=scan.imagery,
        fraud_flags=scan.fraud_flags,
        model_info=scan.model_info,
        candidates_suppressed=scan.candidates_suppressed,
        findings_count=len(rows),
        hypotheses_created=hyp_count,
        created_at=scan.created_at,
        findings=([MlFindingOut.model_validate(f) for f in rows] if include_findings else None),
    )


# ── endpoints ───────────────────────────────────────────────────────────────


@router.get("/health", response_model=MlHealthOut)
async def ml_health(user: User = Depends(get_current_user)) -> MlHealthOut:
    from app.config import settings

    _require_map_user(user)
    configured = bool(settings.ML_BASE_URL and settings.ML_ENABLED)
    try:
        body = await ml_client.health()
    except MlUnavailable as exc:
        return MlHealthOut(
            configured=configured,
            status="unavailable",
            detail=exc.detail,
        )

    return MlHealthOut(
        configured=configured,
        status=str(body.get("status", "ok")),
        backend=body.get("backend"),
        backend_ready=body.get("backend_ready"),
        trained=body.get("trained"),
        version=body.get("version"),
    )


@router.post("/scans", response_model=MlScanOut, status_code=status.HTTP_201_CREATED)
async def create_scan(
    body: MlScanCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MlScanOut:
    _require_map_user(user)

    payload: dict[str, Any] = {
        "bbox": list(body.bbox),
        "zoom": body.zoom,
        "render_overlay": True,
        "overlay_by_class": True,
        "include_geojson": True,
    }
    if body.source:
        payload["source"] = body.source
    if body.territory_id is not None:
        payload["territory_id"] = body.territory_id
    if body.min_confidence is not None:
        payload["min_confidence"] = body.min_confidence
    if body.min_area_px is not None:
        payload["min_area_px"] = body.min_area_px

    try:
        result = await ml_client.detect_area(payload)
    except MlUnavailable as exc:
        _raise_ml(exc)

    imagery = result.get("imagery") or {}
    suppressed = bool(imagery.get("candidates_suppressed"))
    overlay = result.get("overlay") or {}
    summary = result.get("summary") or {}
    detections = result.get("detections") or []
    candidates = result.get("point_candidates") or []
    det_by_id = _detection_lookup(detections)

    org_id = _org_id_for_user(user)
    scan = MlScan(
        id=uuid.uuid4(),
        requester_id=user.id,
        organization_id=org_id,
        bbox=list(body.bbox),
        zoom=body.zoom,
        tile_source=imagery.get("source") or body.source,
        ml_job_id=result.get("job_id"),
        summary=summary,
        geojson=result.get("geojson"),
        overlay_bounds=overlay.get("bounds"),
        imagery=imagery or None,
        fraud_flags=result.get("fraud_flags") or None,
        model_info=result.get("model"),
        candidates_suppressed=suppressed,
    )
    session.add(scan)
    await session.flush()

    # Пустая коллекция без lazy IO — иначе append в async даёт MissingGreenlet.
    set_committed_value(scan, "findings", [])

    # Индекс detection_id → finding для привязки hypothesis из candidates
    findings_by_det: dict[int, MlFinding] = {}
    findings_list: list[MlFinding] = []

    for det in detections:
        if not isinstance(det, dict):
            continue
        det_id = det.get("id")
        centroid = det.get("centroid")
        lat = lon = None
        if isinstance(centroid, (list, tuple)) and len(centroid) >= 2:
            lat, lon = float(centroid[0]), float(centroid[1])

        geom_value = None
        geometry = det.get("geometry")
        if isinstance(geometry, dict) and geometry.get("type"):
            geom_value = func.ST_SetSRID(
                func.ST_GeomFromGeoJSON(json.dumps(geometry)),
                4326,
            )
        elif lat is not None and lon is not None:
            geom_value = ST_SetSRID(ST_MakePoint(lon, lat), 4326)

        finding = MlFinding(
            id=uuid.uuid4(),
            scan_id=scan.id,
            detection_id=int(det_id) if det_id is not None else None,
            lat=lat,
            lon=lon,
            geom=geom_value,
            trash_categories=[det["trash_category"]] if det.get("trash_category") else None,
            dominant_category=det.get("trash_category"),
            fraction=det.get("fraction"),
            confidence=det.get("confidence"),
            estimated_volume_m3=det.get("volume_m3"),
            estimated_mass_kg=det.get("mass_kg"),
            label_ru=det.get("label_ru"),
            color_hex=det.get("color_hex"),
        )
        session.add(finding)
        findings_list.append(finding)
        if det_id is not None:
            findings_by_det[int(det_id)] = finding

    await session.flush()

    hypotheses_created = 0
    if not suppressed:
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            lat = cand.get("lat")
            lon = cand.get("lon")
            if lat is None or lon is None:
                continue
            lat_f, lon_f = float(lat), float(lon)

            point = ST_SetSRID(ST_MakePoint(lon_f, lat_f), 4326)
            cand_org = await _find_org_with_buffer(session, point)
            if cand_org is None:
                cand_org = org_id

            geom_value = None
            geometry = cand.get("geometry")
            if isinstance(geometry, dict) and geometry.get("type"):
                geom_value = func.ST_SetSRID(
                    func.ST_GeomFromGeoJSON(json.dumps(geometry)),
                    4326,
                )

            categories_raw = cand.get("trash_categories") or []
            categories: list[str] = []
            for item in categories_raw:
                cat = _parse_trash_category(str(item))
                if cat is not None:
                    categories.append(cat.value)
            dominant = _parse_trash_category(cand.get("dominant_category"))
            fraction = _parse_fraction(cand.get("fraction"))

            label_bits = []
            for did in cand.get("detection_ids") or []:
                d = det_by_id.get(int(did))
                if d and d.get("label_ru"):
                    label_bits.append(str(d["label_ru"]))
            labels = ", ".join(dict.fromkeys(label_bits)) or "мусор"
            conf = cand.get("confidence")
            conf_s = f"{float(conf):.0%}" if conf is not None else "—"
            description = (
                f"Автодетекция (uav_auto): {labels}, уверенность {conf_s}. "
                "Требует подтверждения сотрудником ООПТ."
            )

            hypothesis = Hypothesis(
                id=uuid.uuid4(),
                author_id=user.id,
                organization_id=cand_org,
                lat=lat_f,
                lon=lon_f,
                location=point,
                geom=geom_value,
                description=description,
                status=HypothesisStatus.pending,
                source=HypothesisSource.uav_auto,
                trash_categories=categories or None,
                dominant_category=dominant,
                fraction=fraction,
                estimated_volume_m3=cand.get("estimated_volume_m3"),
                computed_mass_kg=cand.get("estimated_mass_kg"),
            )
            session.add(hypothesis)
            await session.flush()
            hypotheses_created += 1

            for did in cand.get("detection_ids") or []:
                finding = findings_by_det.get(int(did))
                if finding is not None and finding.hypothesis_id is None:
                    finding.hypothesis_id = hypothesis.id

            await emit(
                session,
                EventType.point_created,
                user_id=user.id,
                lat=lat_f,
                lon=lon_f,
                payload={
                    "hypothesis_id": str(hypothesis.id),
                    "organization_id": str(cand_org) if cand_org else None,
                    "mode": "uav_auto",
                    "ml_scan_id": str(scan.id),
                    "has_photo": False,
                    "trash_categories": categories or None,
                    "dominant_category": dominant.value if dominant else None,
                    "fraction": fraction.value if fraction else None,
                    "volume_m3": cand.get("estimated_volume_m3"),
                    "mass_kg": cand.get("estimated_mass_kg"),
                    "confidence": cand.get("confidence"),
                },
            )
            if cand_org is not None:
                await emit(
                    session,
                    EventType.point_received_in_zone,
                    user_id=user.id,
                    lat=lat_f,
                    lon=lon_f,
                    payload={
                        "hypothesis_id": str(hypothesis.id),
                        "organization_id": str(cand_org),
                        "mode": "uav_auto",
                        "ml_scan_id": str(scan.id),
                    },
                )

    await session.flush()

    out = _scan_out(scan, include_findings=True, findings=findings_list)
    out.hypotheses_created = hypotheses_created
    return out


@router.get("/scans", response_model=MlScanListOut)
async def list_scans(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    organization_id: uuid.UUID | None = None,
) -> MlScanListOut:
    _require_map_user(user)

    query = select(MlScan).order_by(MlScan.created_at.desc())
    count_q = select(func.count()).select_from(MlScan)

    staff_org = _org_id_for_user(user)
    filter_org = organization_id or staff_org
    if filter_org is not None and user.role == UserRole.staff:
        query = query.where(MlScan.organization_id == filter_org)
        count_q = count_q.where(MlScan.organization_id == filter_org)
    elif organization_id is not None and user.role == UserRole.coordinator:
        query = query.where(MlScan.organization_id == organization_id)
        count_q = count_q.where(MlScan.organization_id == organization_id)

    total = int((await session.execute(count_q)).scalar_one())
    rows = (await session.execute(query.offset(offset).limit(limit))).scalars().unique().all()
    items: list[MlScanOut] = []
    for s in rows:
        findings = list(s.findings or [])
        items.append(_scan_out(s, findings=findings))
    return MlScanListOut(items=items, total=total)


@router.get("/scans/{scan_id}", response_model=MlScanOut)
async def get_scan(
    scan_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MlScanOut:
    _require_map_user(user)
    scan = await session.get(MlScan, scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Скан не найден.")
    findings = list(scan.findings or [])
    return _scan_out(scan, include_findings=True, findings=findings)


@router.get("/findings", response_model=MlFindingListOut)
async def list_findings(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    scan_id: uuid.UUID | None = None,
) -> MlFindingListOut:
    _require_map_user(user)

    query = select(MlFinding).order_by(MlFinding.created_at.desc())
    count_q = select(func.count()).select_from(MlFinding)

    if scan_id is not None:
        query = query.where(MlFinding.scan_id == scan_id)
        count_q = count_q.where(MlFinding.scan_id == scan_id)

    staff_org = _org_id_for_user(user)
    if staff_org is not None:
        query = query.join(MlScan).where(MlScan.organization_id == staff_org)
        count_q = count_q.join(MlScan).where(MlScan.organization_id == staff_org)

    total = int((await session.execute(count_q)).scalar_one())
    rows = (await session.execute(query.offset(offset).limit(limit))).scalars().all()
    return MlFindingListOut(
        items=[MlFindingOut.model_validate(r) for r in rows],
        total=total,
    )


@router.get("/overlay.geojson")
async def overlay_geojson(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100),
    scan_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Объединённый GeoJSON последних сканов для слоя на карте."""
    _require_map_user(user)

    query = select(MlScan).order_by(MlScan.created_at.desc())
    if scan_id is not None:
        query = query.where(MlScan.id == scan_id)
    else:
        staff_org = _org_id_for_user(user)
        if staff_org is not None:
            query = query.where(MlScan.organization_id == staff_org)
        query = query.limit(limit)

    rows = (await session.execute(query)).scalars().all()
    features: list[dict[str, Any]] = []
    for scan in rows:
        gj = scan.geojson
        if not isinstance(gj, dict):
            continue
        for feat in gj.get("features") or []:
            if not isinstance(feat, dict):
                continue
            props = dict(feat.get("properties") or {})
            props["ml_scan_id"] = str(scan.id)
            props["scanned_at"] = scan.created_at.isoformat()
            features.append(
                {
                    "type": "Feature",
                    "geometry": feat.get("geometry"),
                    "properties": props,
                }
            )

    return {"type": "FeatureCollection", "features": features}
