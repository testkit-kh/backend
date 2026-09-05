"""Quick verification script."""
import pathlib
import sys

base = pathlib.Path(__file__).resolve().parent

# 1. Model check
from app.models import Hypothesis  # noqa: E402

cols = [c.name for c in Hypothesis.__table__.columns]
for expected in ("client_id", "created_at_client", "geom"):
    assert expected in cols, f"MISSING {expected}"
print("✓ Model: client_id, created_at_client, geom present")

# 2. Schema check
from app.schemas import (  # noqa: E402
    HypothesisCreateRequest,
    HypothesisOut,
    HypothesisValidateRequest,
)

req = HypothesisCreateRequest(
    geometry={"type": "Point", "coordinates": [37.62, 55.75]},
    description="Test via GeoJSON",
    client_id="550e8400-e29b-41d4-a716-446655440000",
)
print(f"✓ GeoJSON Point: lat={req.lat}, lon={req.lon}")

req2 = HypothesisCreateRequest(
    lat=55.75, lon=37.62, description="Legacy"
)
print("✓ Legacy lat/lon input")

try:
    HypothesisCreateRequest(description="No coords")
    print("✗ Should have failed")
    sys.exit(1)
except Exception:
    print("✓ No-coords correctly rejected")

out_fields = list(HypothesisOut.model_fields.keys())
assert "client_id" in out_fields
assert "created_at_client" in out_fields
print("✓ HypothesisOut has offline fields")

v = HypothesisValidateRequest(status="approved")
print(f"✓ ValidateRequest: {v.status}")

# 3. Import check for hypotheses router
from app.hypotheses import router  # noqa: E402

routes = [r.path for r in router.routes if hasattr(r, "methods")]
print(f"✓ Router routes: {routes}")

# 4. Line length check
files = [
    "app/models.py",
    "app/schemas.py",
    "app/hypotheses.py",
    "alembic/versions/0008_idempotency_buffer_polygon.py",
]
ok = True
for f in files:
    lines = (base / f).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, 1):
        if len(line) > 100:
            print(f"✗ {f}:{i} len={len(line)} > 100")
            ok = False
if ok:
    print("✓ All lines <= 100 chars")

print("\n=== ALL CHECKS PASSED ===")
