"""Прогон новых ручек так, как их зовёт фронт."""

from __future__ import annotations

import json
import sys
import uuid
from http.cookiejar import CookieJar

import httpx

BASE = "http://127.0.0.1:8000"
PASS = "password123"

# Аккаунты со стенда
ACCOUNTS = {
    "volunteer": "anya.teen@example.ru",
    "staff": "staff@example.ru",
    "coord": "coord@example.ru",
}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append(("OK", name, detail))
        print(f"OK   {name}" + (f" — {detail}" if detail else ""))

    def fail(self, name: str, detail: str) -> None:
        self.rows.append(("FAIL", name, detail))
        print(f"FAIL {name} — {detail}")

    def skip(self, name: str, detail: str) -> None:
        self.rows.append(("SKIP", name, detail))
        print(f"SKIP {name} — {detail}")


def login(client: httpx.Client, email: str) -> str:
    r = client.post(
        f"{BASE}/auth/login",
        data={"username": email, "password": PASS},
    )
    r.raise_for_status()
    data = r.json()
    assert "access_token" in data
    return data["access_token"]


def main() -> int:
    rep = Report()
    # Сначала — что живой OpenAPI вообще новый
    with httpx.Client(timeout=30.0) as c:
        openapi = c.get(f"{BASE}/openapi.json").json()
        paths = openapi["paths"]
        expected = [
            "/api/v1/uploads/presign",
            "/api/v1/volunteers/me/education",
            "/api/v1/organizations/me/territory",
            "/auth/refresh",
            "/auth/logout",
            "/api/v1/certificates/me",
            "/api/v1/certificates/verify/{code}",
            "/api/v1/certificates/{code}/pdf",
            "/api/v1/certificates/{code}/share",
            "/api/v1/events/{event_id}/before-after",
            "/api/v1/public/points.geojson",
            "/users/me",
        ]
        for p in expected:
            if p in paths:
                rep.ok(f"openapi {p}")
            else:
                rep.fail(f"openapi {p}", "нет в живом OpenAPI — uvicorn старый?")

        if "/auth/refresh" not in paths:
            print("\nСтоп: сервер без новых ручек.")
            return 1

    # --- волонтёр: login + refresh + education + hypotheses + certs ---
    jar = CookieJar()
    with httpx.Client(base_url=BASE, timeout=30.0, cookies=jar) as client:
        try:
            token = login(client, ACCOUNTS["volunteer"])
            rep.ok("POST /auth/login (volunteer)", f"expires_in cookie={bool(jar)}")
        except Exception as e:
            rep.fail("POST /auth/login (volunteer)", str(e))
            return 1

        # refresh без Bearer — только cookie
        r = client.post("/auth/refresh")
        if r.status_code == 200 and "access_token" in r.json():
            token = r.json()["access_token"]
            rep.ok("POST /auth/refresh", f"cookie set={ 'refresh_token' in {c.name for c in jar} }")
        else:
            rep.fail("POST /auth/refresh", f"{r.status_code} {r.text[:200]}")

        h = {"Authorization": f"Bearer {token}"}

        r = client.get("/auth/me", headers=h)
        if r.status_code == 200:
            me = r.json()
            rep.ok("GET /auth/me", f"role={me.get('role')} consent={me.get('consent_status')}")
        else:
            rep.fail("GET /auth/me", f"{r.status_code} {r.text[:200]}")
            me = {}

        # education
        edu_body = {
            "level": "school",
            "institution_name": "Школа №1",
            "institution_inn": None,
            "grade": "10",
            "city": "Петропавловск-Камчатский",
        }
        r = client.post("/api/v1/volunteers/me/education", headers=h, json=edu_body)
        if r.status_code == 200:
            rep.ok("POST /api/v1/volunteers/me/education", r.json().get("level", ""))
        else:
            rep.fail("POST /api/v1/volunteers/me/education", f"{r.status_code} {r.text[:300]}")

        r = client.get("/api/v1/volunteers/me/education", headers=h)
        if r.status_code == 200:
            rep.ok("GET /api/v1/volunteers/me/education")
        else:
            rep.fail("GET /api/v1/volunteers/me/education", f"{r.status_code} {r.text[:200]}")

        # uploads presign (может упасть без MinIO — фиксируем)
        r = client.post(
            "/api/v1/uploads/presign",
            headers=h,
            json={
                "filename": "test.jpg",
                "content_type": "image/jpeg",
                "purpose": "hypothesis_photo",
            },
        )
        if r.status_code == 200:
            body = r.json()
            need = {"upload_url", "public_url", "headers", "method", "expires_in", "key"}
            missing = need - set(body)
            if missing:
                rep.fail("POST /uploads/presign shape", f"нет полей {missing}")
            elif body.get("method", "PUT").upper() != "PUT":
                rep.fail("POST /uploads/presign method", body.get("method"))
            else:
                rep.ok("POST /api/v1/uploads/presign", f"method={body.get('method')}")
                # PUT в MinIO — опционально (на стенде часто нет)
                try:
                    put = httpx.put(
                        body["upload_url"],
                        content=b"\xff\xd8\xff\xd9",
                        headers=body.get("headers") or {"Content-Type": "image/jpeg"},
                        timeout=5.0,
                    )
                    if put.status_code in (200, 204):
                        rep.ok("PUT to MinIO (presigned)", str(put.status_code))
                    else:
                        rep.skip(
                            "PUT to MinIO (presigned)",
                            f"{put.status_code} — бакет/политики?",
                        )
                except httpx.HTTPError as exc:
                    rep.skip("PUT to MinIO (presigned)", f"{type(exc).__name__}: MinIO не поднят")
        else:
            rep.fail("POST /api/v1/uploads/presign", f"{r.status_code} {r.text[:300]}")

        # hypotheses create with client_id (как offlineQueue)
        client_id = str(uuid.uuid4())
        hyp_body = {
            "lat": 54.85,
            "lon": 161.75,
            "description": "Проверка фронтовой очереди",
            "client_id": client_id,
            "created_at_client": "2026-09-05T12:00:00Z",
            "trash": {
                "dominant_category": "plastic",
                "trash_categories": ["plastic"],
                "estimated_volume_m3": 0.5,
                "access_type": "on_foot",
            },
        }
        r = client.post("/api/v1/hypotheses", headers=h, json=hyp_body)
        if r.status_code in (200, 201):
            hyp = r.json()
            rep.ok(
                "POST /api/v1/hypotheses (+client_id, trash)",
                f"{r.status_code} id={hyp.get('id')}",
            )
            # идемпотентность
            r2 = client.post("/api/v1/hypotheses", headers=h, json=hyp_body)
            if r2.status_code == 200 and r2.json().get("id") == hyp.get("id"):
                rep.ok("POST /hypotheses idempotent client_id", "200 same id")
            else:
                rep.fail(
                    "POST /hypotheses idempotent client_id",
                    f"{r2.status_code} {r2.text[:200]}",
                )
        else:
            rep.fail("POST /api/v1/hypotheses", f"{r.status_code} {r.text[:400]}")
            hyp = {}

        r = client.get("/api/v1/hypotheses/my", headers=h, params={"limit": 100})
        if r.status_code == 200:
            items = r.json().get("items", r.json() if isinstance(r.json(), list) else [])
            rep.ok("GET /api/v1/hypotheses/my", f"n={len(items) if isinstance(items, list) else '?'}")
        else:
            rep.fail("GET /api/v1/hypotheses/my", f"{r.status_code} {r.text[:200]}")

        # certificates — пока может не быть выдан
        r = client.get("/api/v1/certificates/me", headers=h)
        if r.status_code == 200:
            cert = r.json()
            rep.ok("GET /certificates/me", cert.get("code", ""))
            code = cert["code"]
            rv = client.get(f"/api/v1/certificates/verify/{code}")
            if rv.status_code == 200 and rv.json().get("valid"):
                rep.ok("GET /certificates/verify/{code}", "valid")
            else:
                rep.fail("GET /certificates/verify/{code}", f"{rv.status_code} {rv.text[:200]}")
            pdf = client.get(f"/api/v1/certificates/{code}/pdf")
            if pdf.status_code == 200 and pdf.headers.get("content-type", "").startswith(
                "application/pdf"
            ):
                rep.ok("GET /certificates/{code}/pdf", f"{len(pdf.content)} bytes")
            else:
                rep.fail("GET /certificates/{code}/pdf", f"{pdf.status_code}")
            sh = client.post(f"/api/v1/certificates/{code}/share", headers=h)
            if sh.status_code == 200:
                rep.ok("POST /certificates/{code}/share")
            else:
                rep.fail("POST /certificates/{code}/share", f"{sh.status_code} {sh.text[:200]}")
        elif r.status_code == 404:
            rep.skip("GET /certificates/me", "ещё не выдан — прогоним через coord approve")
            cert = None
        else:
            rep.fail("GET /certificates/me", f"{r.status_code} {r.text[:200]}")
            cert = None

        # events list / join
        r = client.get("/api/v1/events", headers=h)
        if r.status_code == 200:
            events = r.json().get("items", [])
            rep.ok("GET /api/v1/events (volunteer)", f"n={len(events)}")
            if events:
                eid = events[0]["id"]
                j = client.post(f"/api/v1/events/{eid}/join", headers=h)
                if j.status_code in (200, 201):
                    rep.ok("POST /events/{id}/join", str(j.status_code))
                    lv = client.delete(f"/api/v1/events/{eid}/join", headers=h)
                    if lv.status_code in (200, 204):
                        rep.ok("DELETE /events/{id}/join", str(lv.status_code))
                    else:
                        rep.fail("DELETE /events/{id}/join", f"{lv.status_code} {lv.text[:200]}")
                else:
                    rep.fail("POST /events/{id}/join", f"{j.status_code} {j.text[:300]}")
        else:
            rep.fail("GET /api/v1/events", f"{r.status_code} {r.text[:200]}")

        # logout
        r = client.post("/auth/logout")
        if r.status_code in (200, 204):
            rep.ok("POST /auth/logout")
        else:
            rep.fail("POST /auth/logout", f"{r.status_code} {r.text[:200]}")

    # --- staff: territory + parcels + queue ---
    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        try:
            token = login(client, ACCOUNTS["staff"])
            h = {"Authorization": f"Bearer {token}"}
            rep.ok("POST /auth/login (staff)")
        except Exception as e:
            rep.fail("POST /auth/login (staff)", str(e))
            token = None
            h = {}

        if token:
            r = client.get("/auth/me", headers=h)
            org = r.json().get("organization", {}) if r.status_code == 200 else {}
            rep.ok(
                "staff org profile",
                f"name={org.get('name')} has_territory={org.get('has_territory')}",
            )

            # territory without cadastral (OSM geometry) — маленький квадрат у Кроноцкого
            geom = {
                "type": "Polygon",
                "coordinates": [
                    [
                        [161.70, 54.80],
                        [161.80, 54.80],
                        [161.80, 54.90],
                        [161.70, 54.90],
                        [161.70, 54.80],
                    ]
                ],
            }
            r = client.patch(
                "/api/v1/organizations/me/territory",
                headers=h,
                json={
                    "source": "osm",
                    "osm_id": "relation/test",
                    "name": "Тестовый полигон",
                    "geometry": geom,
                },
            )
            if r.status_code == 200:
                rep.ok(
                    "PATCH /organizations/me/territory",
                    f"has_territory={r.json().get('has_territory')}",
                )
            else:
                rep.fail(
                    "PATCH /organizations/me/territory",
                    f"{r.status_code} {r.text[:400]}",
                )

            r = client.get("/api/v1/organizations/me", headers=h)
            if r.status_code == 200:
                rep.ok("GET /organizations/me", f"territory_source={r.json().get('territory_source')}")
            else:
                rep.fail("GET /organizations/me", f"{r.status_code}")

            r = client.get("/api/v1/hypotheses/pending", headers=h)
            if r.status_code == 200:
                pending = r.json()
                n = len(pending) if isinstance(pending, list) else "?"
                rep.ok("GET /hypotheses/pending", f"n={n}")
            else:
                rep.fail("GET /hypotheses/pending", f"{r.status_code} {r.text[:200]}")

            r = client.get("/api/v1/analytics/summary", headers=h)
            if r.status_code == 200:
                rep.ok("GET /analytics/summary")
            else:
                rep.fail("GET /analytics/summary", f"{r.status_code} {r.text[:200]}")

            for slug in ("oopt", "impact"):
                r = client.get(f"/api/v1/analytics/embed/{slug}", headers=h)
                if r.status_code == 200:
                    rep.ok(f"GET /analytics/embed/{slug} (staff)")
                elif r.status_code == 503:
                    rep.skip(f"GET /analytics/embed/{slug} (staff)", "Metabase не запровижен")
                else:
                    rep.fail(
                        f"GET /analytics/embed/{slug} (staff)",
                        f"{r.status_code} {r.text[:200]}",
                    )

    # --- coord: approve certificate if pending ---
    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        try:
            token = login(client, ACCOUNTS["coord"])
            h = {"Authorization": f"Bearer {token}"}
            rep.ok("POST /auth/login (coord)")
        except Exception as e:
            rep.fail("POST /auth/login (coord)", str(e))
            token = None
            h = {}

        if token:
            # submit cert as volunteer first if needed
            with httpx.Client(base_url=BASE, timeout=30.0) as vc:
                vt = login(vc, ACCOUNTS["volunteer"])
                vh = {"Authorization": f"Bearer {vt}"}
                me = vc.get("/auth/me", headers=vh).json()
                if me.get("certificate_status") in (None, "none", "rejected"):
                    sr = vc.post(
                        "/api/v1/volunteers/me/certificate",
                        headers=vh,
                        json={"certificate_url": "https://zaprirodu.ispring.ru/example"},
                    )
                    if sr.status_code == 200:
                        rep.ok("POST /volunteers/me/certificate", "submitted")
                    else:
                        rep.fail(
                            "POST /volunteers/me/certificate",
                            f"{sr.status_code} {sr.text[:300]}",
                        )
                else:
                    rep.ok(
                        "volunteer certificate_status",
                        str(me.get("certificate_status")),
                    )

            r = client.get("/api/v1/certificates/pending", headers=h)
            if r.status_code == 200:
                pending = r.json()
                rep.ok("GET /certificates/pending", f"n={len(pending)}")
                if pending:
                    vid = pending[0]["volunteer_id"]
                    rv = client.post(
                        f"/api/v1/certificates/{vid}/review",
                        headers=h,
                        json={"approved": True},
                    )
                    if rv.status_code == 200:
                        rep.ok("POST /certificates/{id}/review approve")
                        # теперь me у волонтёра
                        with httpx.Client(base_url=BASE, timeout=30.0) as vc:
                            vt = login(vc, ACCOUNTS["volunteer"])
                            cm = vc.get(
                                "/api/v1/certificates/me",
                                headers={"Authorization": f"Bearer {vt}"},
                            )
                            if cm.status_code == 200:
                                code = cm.json()["code"]
                                rep.ok("GET /certificates/me after approve", code)
                                vf = vc.get(f"/api/v1/certificates/verify/{code}")
                                body = vf.json()
                                if body.get("valid") and not body.get("revoked"):
                                    rep.ok(
                                        "verify after issue",
                                        body.get("full_name", ""),
                                    )
                                else:
                                    rep.fail("verify after issue", json.dumps(body)[:200])
                            else:
                                rep.fail(
                                    "GET /certificates/me after approve",
                                    f"{cm.status_code} {cm.text[:300]}",
                                )
                    else:
                        rep.fail(
                            "POST /certificates/{id}/review",
                            f"{rv.status_code} {rv.text[:300]}",
                        )
            else:
                rep.fail("GET /certificates/pending", f"{r.status_code} {r.text[:200]}")

            r = client.get("/api/v1/analytics/embed/funnel", headers=h)
            if r.status_code == 200:
                rep.ok("GET /analytics/embed/funnel (coord)")
            elif r.status_code == 503:
                rep.skip("GET /analytics/embed/funnel (coord)", "Metabase не запровижен")
            else:
                rep.fail(
                    "GET /analytics/embed/funnel (coord)",
                    f"{r.status_code} {r.text[:200]}",
                )

            # Уже approved до миграции 0015 — /certificates/me должен догнать выдачу
            with httpx.Client(base_url=BASE, timeout=30.0) as vc:
                vt = login(vc, ACCOUNTS["volunteer"])
                cm = vc.get(
                    "/api/v1/certificates/me",
                    headers={"Authorization": f"Bearer {vt}"},
                )
                if cm.status_code == 200:
                    code = cm.json()["code"]
                    rep.ok("GET /certificates/me (backfill)", code)
                    vf = vc.get(f"/api/v1/certificates/verify/{code}")
                    body = vf.json()
                    if body.get("valid") and not body.get("revoked"):
                        rep.ok("verify after backfill", body.get("full_name", ""))
                    else:
                        rep.fail("verify after backfill", json.dumps(body)[:200])
                    pdf = vc.get(f"/api/v1/certificates/{code}/pdf")
                    if pdf.status_code == 200 and "pdf" in pdf.headers.get("content-type", ""):
                        rep.ok("GET /certificates/{code}/pdf", f"{len(pdf.content)} B")
                    else:
                        rep.fail("GET /certificates/{code}/pdf", f"{pdf.status_code}")
                    sh = vc.post(
                        f"/api/v1/certificates/{code}/share",
                        headers={"Authorization": f"Bearer {vt}"},
                    )
                    if sh.status_code == 200:
                        rep.ok("POST /certificates/{code}/share")
                    else:
                        rep.fail(
                            "POST /certificates/{code}/share",
                            f"{sh.status_code} {sh.text[:200]}",
                        )
                else:
                    rep.fail(
                        "GET /certificates/me (backfill)",
                        f"{cm.status_code} {cm.text[:300]}",
                    )

    # public
    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        r = client.get("/api/v1/public/points.geojson")
        if r.status_code == 200:
            rep.ok("GET /public/points.geojson", f"features={len(r.json().get('features', []))}")
        else:
            rep.fail("GET /public/points.geojson", f"{r.status_code}")

        r = client.get("/api/v1/map/layers")
        # may need auth - check
        if r.status_code in (200, 401, 403):
            if r.status_code == 200:
                rep.ok("GET /map/layers", f"features={len(r.json().get('features', []))}")
            else:
                # with volunteer token
                t = login(client, ACCOUNTS["volunteer"])
                r2 = client.get(
                    "/api/v1/map/layers",
                    headers={"Authorization": f"Bearer {t}"},
                )
                if r2.status_code == 200:
                    rep.ok("GET /map/layers (auth)", f"n={len(r2.json().get('features', []))}")
                else:
                    rep.fail("GET /map/layers", f"{r2.status_code} {r2.text[:200]}")

    fails = sum(1 for s, _, _ in rep.rows if s == "FAIL")
    oks = sum(1 for s, _, _ in rep.rows if s == "OK")
    skips = sum(1 for s, _, _ in rep.rows if s == "SKIP")
    print(f"\n=== Итого: {oks} ok, {fails} fail, {skips} skip ===")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
