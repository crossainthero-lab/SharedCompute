"""End to end over real sockets: uvicorn serves the marketplace, the real agent loop connects with
its urllib client, claims a job, executes the pipeline-check workload, and the server verifies and
bills it. Only GPU detection is stubbed, so the test behaves the same on machines without GPUs."""
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from conftest import CSRF, PASSWORD
from sharedcompute.api import create_app
from sharedcompute.config import Settings
from sharedcompute_agent.agent import Agent
from sharedcompute_agent.client import ApiError, MarketplaceClient
from sharedcompute_agent.gpu import GpuInfo


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    settings = Settings(db_path=tmp_path / "e2e.sqlite3", password_iterations=1_000, hashchain_iterations=20_000,
                        tick_interval_seconds=0.2, run_background_loop=True)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def wait_for(fn, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


def test_agent_executes_and_server_verifies_over_http(live_server):
    provider = httpx.Client(base_url=live_server, headers=CSRF)
    customer = httpx.Client(base_url=live_server, headers=CSRF)
    provider.post("/api/v1/auth/register", json={"email": "p@example.com", "password": PASSWORD, "display_name": "Provider"})
    customer.post("/api/v1/auth/register", json={"email": "c@example.com", "password": PASSWORD, "display_name": "Customer"})
    customer.post("/api/v1/wallet/demo-topup", json={"amount_usd": "10"})
    token = provider.post("/api/v1/provider/nodes", json={"kind": "agent", "name": "E2E node", "region": "nl-ams",
                                                          "hourly_usd": "0.50"}).json()["agent_token"]

    fake_gpu = GpuInfo(slot=0, vendor="NVIDIA", model="Test GPU", vram_mb=8192, detection="nvidia-smi",
                       fingerprint="0f" * 16, cuda=True)
    agent = Agent(MarketplaceClient(live_server, token), poll_seconds=0.2, max_jobs=1,
                  detector=lambda: [fake_gpu], metrics=lambda: [], capabilities=lambda: ["agent.hashchain"])
    agent_thread = threading.Thread(target=agent.run, daemon=True)
    agent_thread.start()

    wait_for(lambda: any(g["execution"] == "agent" and g["status"] == "available"
                         for g in customer.get("/api/v1/marketplace").json()["gpus"]))
    job = customer.post("/api/v1/jobs", json={"name": "E2E check", "workload": "agent.hashchain", "min_vram_gb": 8,
                                              "est_minutes": 1, "max_budget_usd": "1.00", "region": "nl-ams"}).json()["job"]
    assert job["gpu"]["model"] == "Test GPU"

    done = wait_for(lambda: (j := customer.get(f"/api/v1/jobs/{job['id']}").json()["job"])["status"] == "completed" and j)
    assert done["verified"] is True
    assert done["result"]["output"]["iterations"] == 20_000
    assert done["cost_cents"] >= 1 and done["provider_cents"] + done["platform_cents"] == done["cost_cents"]
    kinds = [e["kind"] for e in done["events"]]
    assert kinds == ["submitted", "assigned", "started", "completed"]

    agent_thread.join(timeout=10)
    assert not agent_thread.is_alive() and agent.jobs_done == 1
    assert provider.get("/api/v1/me").json()["balances"]["earnings_cents"] == done["provider_cents"]


def test_agent_with_revoked_token_stops(live_server):
    agent = Agent(MarketplaceClient(live_server, "scn_" + "z" * 43), poll_seconds=0.1,
                  detector=lambda: [], metrics=lambda: [], capabilities=lambda: [])
    with pytest.raises(ApiError) as err:
        agent.run()
    assert err.value.status == 401


def test_agent_backs_off_when_server_is_unreachable():
    agent = Agent(MarketplaceClient(f"http://127.0.0.1:{free_port()}", "scn_x", timeout=1), poll_seconds=0.1,
                  detector=lambda: [], metrics=lambda: [], capabilities=lambda: [])
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()
    time.sleep(1.5)
    agent.stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
