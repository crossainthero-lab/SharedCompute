"""HTTP-level behaviour: error shapes, limits, headers and static UI serving."""
from fastapi.testclient import TestClient

from conftest import CSRF


def test_health_and_meta(anon, app):
    assert anon.get("/api/health").json()["ok"] is True
    meta = anon.get("/api/v1/meta").json()
    assert meta["platform_fee_bps"] == 1000
    assert {w["id"] for w in meta["workloads"]} >= {"sim.image", "agent.hashchain", "agent.cuda_matmul"}
    assert any(r["code"] == "au-syd" for r in meta["regions"])


def test_unknown_api_paths_return_json_404(anon):
    for method in ("get", "post", "delete"):
        r = getattr(anon, method)("/api/v1/does-not-exist")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_validation_errors_have_stable_shape(alice):
    r = alice.post("/api/v1/jobs", json={"name": "x"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "invalid_input" and err["field"] and err["message"]
    r = alice.post("/api/v1/jobs", json={"name": "Typo", "workload": "sim.image", "min_vram_gb": 12,
                                         "est_minutes": 60, "max_budget_usd": "1", "regoin": "any"})
    assert r.status_code == 422 and r.json()["error"]["field"] == "regoin"
    assert alice.post("/api/v1/jobs", content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 422


def test_oversized_bodies_are_rejected(alice):
    r = alice.post("/api/v1/jobs", content=b"x" * (65 * 1024), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"


def test_security_headers(anon):
    page = anon.get("/")
    assert page.status_code == 200 and "<title>SharedCompute</title>" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    api = anon.get("/api/v1/meta")
    assert api.headers["cache-control"] == "no-store"


def test_static_assets_are_served(anon):
    for path in ("/app.css", "/js/app.js", "/js/views/jobs.js", "/favicon.svg"):
        assert anon.get(path).status_code == 200, path


def test_session_cookie_flags(app):
    client = TestClient(app, headers=CSRF)
    r = client.post("/api/v1/auth/register", json={"email": "c@example.com", "password": "correct horse battery",
                                                   "display_name": "Cookie"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie


def test_prototype_database_is_refused(tmp_path):
    import sqlite3

    import pytest

    from sharedcompute.db import Database

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE providers (id INTEGER)")
    with pytest.raises(RuntimeError, match="v0.1 prototype"):
        Database(path).migrate()
