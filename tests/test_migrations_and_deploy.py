"""Schema migrations and the exposure policy for servers reachable from other machines."""
import io
import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import CSRF, PASSWORD
from sharedcompute import __main__ as server_cli
from sharedcompute.api import create_app
from sharedcompute.config import Settings
from sharedcompute.db import SCHEMA_VERSION, Database, migrations


def test_migrations_are_contiguous():
    assert [v for v, _ in migrations()] == list(range(1, SCHEMA_VERSION + 1)) and SCHEMA_VERSION >= 2


def test_v02_database_upgrades_in_place(tmp_path):
    path = tmp_path / "v02.sqlite3"
    db = Database(path)
    assert db.migrate(target=1) == [1]
    with sqlite3.connect(path) as conn:  # a v0.2 deployment with one simulated and one agent device
        conn.executescript("""
            INSERT INTO users(id, email, display_name, created_at) VALUES (1, 'p@example.com', 'P', 0);
            INSERT INTO nodes(id, owner_id, name, region, kind, default_hourly_cents, capabilities, created_at)
              VALUES (1, 1, 'Sim', 'au-syd', 'simulated', 30, '["sim.image"]', 0),
                     (2, 1, 'Box', 'au-syd', 'agent', 30, '["agent.hashchain"]', 0);
            INSERT INTO gpus(node_id, slot, vendor, model, vram_mb, detection, hourly_cents, created_at)
              VALUES (1, 0, 'NVIDIA', 'RTX 3060', 12288, 'self_reported', 30, 0),
                     (2, 0, 'NVIDIA', 'GTX 1060', 6144, 'nvml', 30, 0);
            INSERT INTO jobs(owner_id, name, workload, execution, min_vram_mb, est_minutes, max_budget_cents, region,
                             status, created_at) VALUES (1, 'old', 'sim.image', 'simulated', 0, 1, 1, 'any', 'pending', 0);
        """)
    assert Database(path).migrate() == list(range(2, SCHEMA_VERSION + 1))
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        sim, agent = conn.execute("SELECT * FROM gpus ORDER BY node_id").fetchall()
        assert (sim["offered"], sim["capabilities"], sim["device_type"]) == (1, '["sim.image"]', "gpu")
        assert agent["offered"] == 0 and "Reconnect" in agent["offer_note"]  # must be re-offered by a v0.3 agent
        assert conn.execute("SELECT name, executed_on FROM jobs").fetchone()["name"] == "old"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert Database(path).migrate() == []  # idempotent


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="Upgrade SharedCompute"):
        Database(path).migrate()


# --- Exposure policy ---------------------------------------------------------------------------------

def test_loopback_keeps_development_defaults():
    s = Settings()
    assert server_cli.apply_bind_policy(s, "127.0.0.1", tls=False, behind_proxy=False, env={}) == []
    assert s.registration_open and s.public_marketplace and not s.cookie_secure


def test_public_bind_without_tls_is_refused():
    with pytest.raises(server_cli.PolicyError, match="without TLS"):
        server_cli.apply_bind_policy(Settings(), "0.0.0.0", tls=False, behind_proxy=False, env={})


@pytest.mark.parametrize("host,tls,proxy", [("0.0.0.0", True, False), ("192.168.1.20", True, False),
                                            ("127.0.0.1", False, True)])
def test_exposed_servers_are_locked_down(host, tls, proxy):
    s = Settings()
    notes = server_cli.apply_bind_policy(s, host, tls=tls, behind_proxy=proxy, env={})
    assert s.cookie_secure and not s.registration_open and not s.public_marketplace and not s.expose_api_docs
    assert any("add-user" in n for n in notes)


def test_operator_can_reopen_explicitly():
    s = Settings()
    server_cli.apply_bind_policy(s, "0.0.0.0", tls=True, behind_proxy=False,
                                 env={"SHAREDCOMPUTE_REGISTRATION": "open", "SHAREDCOMPUTE_PUBLIC_MARKETPLACE": "1"})
    assert s.registration_open and s.public_marketplace


def test_serve_cli_refuses_and_validates(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SHAREDCOMPUTE_DB", str(tmp_path / "x.sqlite3"))
    assert server_cli.main(["serve", "--host", "0.0.0.0"]) == 2
    assert "without TLS" in capsys.readouterr().err
    assert server_cli.main(["serve", "--tls-cert", "only-cert.pem"]) == 2


@pytest.fixture
def locked_app(tmp_path):
    s = Settings(db_path=tmp_path / "locked.sqlite3", password_iterations=1_000, run_background_loop=False)
    server_cli.apply_bind_policy(s, "0.0.0.0", tls=True, behind_proxy=False, env={})
    return create_app(s)


def test_locked_down_server_behaviour(locked_app):
    anon = TestClient(locked_app, headers=CSRF)
    r = anon.post("/api/v1/auth/register", json={"email": "x@example.com", "password": PASSWORD, "display_name": "Xa"})
    assert r.status_code == 403 and "closed" in r.json()["error"]["message"]
    assert anon.get("/api/v1/marketplace").status_code == 401
    assert anon.post("/api/v1/quotes", json={"workload": "sim.image", "min_vram_gb": 1, "est_minutes": 1,
                                             "max_budget_usd": "1"}).status_code == 401
    assert anon.get("/api/docs").status_code in (401, 404) or "swagger" not in anon.get("/api/docs").text.lower()
    meta = anon.get("/api/v1/meta").json()
    assert meta["registration_open"] is False and meta["public_marketplace"] is False


def test_add_user_cli_works_when_registration_is_closed(tmp_path, monkeypatch, capsys, locked_app):
    monkeypatch.setenv("SHAREDCOMPUTE_DB", str(locked_app.state.settings.db_path))
    monkeypatch.setenv("SHAREDCOMPUTE_PASSWORD_ITERATIONS", "1000")
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert server_cli.main(["add-user", "stefan@example.com", "--name", "Stefan", "--operator", "--password-stdin"]) == 0
    assert "(operator)" in capsys.readouterr().out
    # Exposed servers mark the session cookie Secure, so it only travels over HTTPS.
    assert "secure" in TestClient(locked_app, headers=CSRF).post(
        "/api/v1/auth/login", json={"email": "stefan@example.com", "password": PASSWORD}).headers["set-cookie"].lower()
    client = TestClient(locked_app, headers=CSRF, base_url="https://testserver")
    assert client.post("/api/v1/auth/login", json={"email": "stefan@example.com", "password": PASSWORD}).status_code == 200
    assert client.get("/api/v1/marketplace").status_code == 200
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert server_cli.main(["add-user", "bad@example.com", "--name", "Bad", "--password-stdin"]) == 1
