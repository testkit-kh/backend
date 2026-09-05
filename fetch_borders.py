"""
Границы, которых нет в текущем сиде: Дагестанский заповедник и берега озёр.

Nominatim не отдал полигон Дагестанского заповедника. В OSM natural=coastline
есть только у морей — Байкал и Ладога там отсутствуют, их берег это
natural=water. Этот скрипт забирает оба слоя из Overpass и кладёт GeoJSON,
который потом заливается миграцией или сидером.

Запуск:
    python fetch_borders.py
    python fetch_borders.py --out data/borders
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

TIMEOUT_SECONDS = 180

# Дагестанский — по названию: у заповедника несколько тегов в OSM.
# Озёра — строго natural=water, не coastline.
TARGETS: dict[str, str] = {
    "dagestan_reserve": """
[out:json][timeout:180];
(
  relation["boundary"="protected_area"]["name"~"Дагестанский", i];
  relation["leisure"="nature_reserve"]["name"~"Дагестанский", i];
  relation["boundary"="protected_area"]["name:en"~"Dagestan", i]["protect_class"~"1|1a|1b"];
  way["boundary"="protected_area"]["name"~"Дагестанский", i];
);
out geom;
""",
    "lake_baikal": """
[out:json][timeout:180];
(
  relation["natural"="water"]["name"="Байкал"];
  relation["natural"="water"]["name:ru"="Байкал"];
  relation["natural"="water"]["name:en"="Lake Baikal"];
);
out geom;
""",
    "lake_ladoga": """
[out:json][timeout:180];
(
  relation["natural"="water"]["name"="Ладожское озеро"];
  relation["natural"="water"]["name:ru"="Ладожское озеро"];
  relation["natural"="water"]["name:en"="Lake Ladoga"];
);
out geom;
""",
}


def _lonlat(points: list[dict]) -> list[list[float]]:
    return [[float(p["lon"]), float(p["lat"])] for p in points]


def _way_geometry(element: dict) -> dict | None:
    geom = element.get("geometry") or []
    if len(geom) < 2:
        return None
    coords = _lonlat(geom)
    closed = coords[0] == coords[-1] and len(coords) >= 4
    if closed:
        return {"type": "Polygon", "coordinates": [coords]}
    return {"type": "LineString", "coordinates": coords}


def _relation_geometry(element: dict) -> dict | None:
    """Собирает MultiPolygon из outer/inner членов relation.

    Overpass `out geom` кладёт координаты на сами members — отдельных
    запросов к ways не нужно.
    """
    outers: list[list[list[list[float]]]] = []
    inners: list[list[list[float]]] = []
    for member in element.get("members") or []:
        if member.get("type") != "way":
            continue
        geom = member.get("geometry") or []
        if len(geom) < 4:
            continue
        coords = _lonlat(geom)
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        role = member.get("role") or "outer"
        if role == "inner":
            inners.append(coords)
        else:
            outers.append([coords])
    if not outers:
        return None
    if inners and len(outers) == 1:
        outers[0].extend(inners)
    return {"type": "MultiPolygon", "coordinates": outers}


def element_to_feature(element: dict) -> dict | None:
    kind = element.get("type")
    if kind == "way":
        geometry = _way_geometry(element)
    elif kind == "relation":
        geometry = _relation_geometry(element)
    else:
        return None
    if geometry is None:
        return None
    tags = element.get("tags") or {}
    return {
        "type": "Feature",
        "properties": {
            "osm_type": kind,
            "osm_id": element.get("id"),
            "name": tags.get("name") or tags.get("name:ru") or tags.get("name:en"),
            "tags": tags,
        },
        "geometry": geometry,
    }


def overpass_to_geojson(payload: dict) -> dict:
    features = []
    for element in payload.get("elements") or []:
        feature = element_to_feature(element)
        if feature is not None:
            features.append(feature)
    return {"type": "FeatureCollection", "features": features}


def fetch_overpass(query: str) -> dict:
    last_error: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            response = httpx.post(
                url,
                content=query.encode("utf-8"),
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "User-Agent": "eco-project-border-fetch/1.0",
                },
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            last_error = error
            print(f"Overpass {url} не ответил: {error}", file=sys.stderr)
    raise SystemExit(f"Ни одно зеркало Overpass не ответило: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Скачать границы ООПТ и озёр из OSM")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/borders"),
        help="Каталог для .geojson (по умолчанию data/borders)",
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    combined: list[dict] = []
    for name, query in TARGETS.items():
        print(f"Запрашиваю {name}…")
        raw = fetch_overpass(query)
        geojson = overpass_to_geojson(raw)
        path = args.out / f"{name}.geojson"
        path.write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {path}: {len(geojson['features'])} объектов")
        for feature in geojson["features"]:
            props = feature.setdefault("properties", {})
            props["source_file"] = name
            combined.append(feature)

    all_path = args.out / "all.geojson"
    all_path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": combined},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Сводный файл: {all_path} ({len(combined)} объектов)")


if __name__ == "__main__":
    main()
