import sys
from pathlib import Path

import pytest

# Agent-side test helpers (fake libcuda) live with the agent's own tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent" / "tests"))

from fastapi.testclient import TestClient

from sharedcompute.api import create_app
from sharedcompute.config import Settings

CSRF = {"X-Requested-With": "SharedCompute"}
PASSWORD = "correct horse battery"


class FakeClock:
    def __init__(self, start: float = 1_760_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path, clock: FakeClock) -> Settings:
    return Settings(db_path=tmp_path / "test.sqlite3", password_iterations=1_000, run_background_loop=False,
                    hashchain_iterations=2_000, clock=clock, login_attempts_per_minute=1_000)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def anon(app):
    return TestClient(app, headers=CSRF)


def make_user(app, email: str, name: str = "Test User", credits: str | None = "100.00") -> TestClient:
    client = TestClient(app, headers=CSRF)
    r = client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD, "display_name": name})
    assert r.status_code == 201, r.text
    if credits:
        assert client.post("/api/v1/wallet/demo-topup", json={"amount_usd": credits}).status_code == 200
    return client


@pytest.fixture
def alice(app):
    return make_user(app, "alice@example.com", "Alice")


@pytest.fixture
def bob(app):
    return make_user(app, "bob@example.com", "Bob")


def tick(app):
    """Run one scheduler tick, as the background loop would."""
    from sharedcompute.services import scheduler

    with app.state.db.write() as conn:
        scheduler.tick(conn, app.state.settings)


def job_body(**overrides) -> dict:
    body = {"name": "Test batch", "workload": "sim.image", "min_vram_gb": 12, "est_minutes": 60,
            "max_budget_usd": "2.00", "region": "any"}
    body.update(overrides)
    return body


def submit(client: TestClient, **overrides) -> dict:
    r = client.post("/api/v1/jobs", json=job_body(**overrides))
    assert r.status_code == 201, r.text
    return r.json()["job"]


def get_job(client: TestClient, job_id: int) -> dict:
    r = client.get(f"/api/v1/jobs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()["job"]


def balances(client: TestClient) -> dict:
    return client.get("/api/v1/me").json()["balances"]


def ledger_invariants(app) -> None:
    """The ledger always balances and no customer balance is negative."""
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM ledger_postings").fetchone()[0] == 0
        for (entry_id,) in conn.execute("SELECT id FROM ledger_entries"):
            total = conn.execute("SELECT SUM(amount_cents) FROM ledger_postings WHERE entry_id = ?",
                                 (entry_id,)).fetchone()[0]
            assert total == 0, f"entry {entry_id} unbalanced"
        negative = conn.execute(
            """SELECT a.code, SUM(p.amount_cents) AS bal FROM ledger_accounts a JOIN ledger_postings p
               ON p.account_id = a.id WHERE a.kind IN ('available', 'held', 'earnings')
               GROUP BY a.id HAVING bal < 0""").fetchall()
        assert not negative, [dict(r) for r in negative]
        held = conn.execute(
            """SELECT COALESCE(SUM(p.amount_cents), 0) FROM ledger_postings p JOIN ledger_accounts a
               ON a.id = p.account_id WHERE a.kind = 'held'""").fetchone()[0]
        open_jobs = conn.execute(
            "SELECT COALESCE(SUM(max_budget_cents), 0) FROM jobs WHERE status IN ('pending','assigned','running')"
        ).fetchone()[0]
        assert held == open_jobs, "held funds must equal the budgets of unfinished jobs"
        revenue = conn.execute(
            """SELECT COALESCE(SUM(p.amount_cents), 0) FROM ledger_postings p JOIN ledger_accounts a
               ON a.id = p.account_id WHERE a.kind = 'revenue'""").fetchone()[0]
        fees = conn.execute("SELECT COALESCE(SUM(platform_cents), 0) FROM jobs WHERE status = 'completed'").fetchone()[0]
        assert revenue == fees
