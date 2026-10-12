"""End to end over real sockets: uvicorn serves the marketplace (plain HTTP on loopback, and HTTPS with a
private CA), the real agent loop connects with its urllib client, claims jobs, executes them and the server
verifies and bills them.

What is real here: HTTP/TLS, the agent loop, scheduling, the CPU workload, verification, ledger.
What is stubbed: device detection, and for the GPU test the CUDA driver (fake_cuda.FakeCuda), so these
tests pass on machines without an NVIDIA GPU. They do not show that the PTX runs on real hardware.
"""
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from conftest import CSRF, PASSWORD
from fake_cuda import FakeCuda
from sharedcompute import workloads as server_workloads
from sharedcompute.api import create_app
from sharedcompute.config import Settings
from sharedcompute_agent import cuda
from sharedcompute_agent.agent import Agent
from sharedcompute_agent.client import ApiError, MarketplaceClient
from sharedcompute_agent.gpu import GpuInfo
from sharedcompute_agent.policy import OfferPolicy
from sharedcompute_agent.workloads import RunOptions

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(tmp_path, **tls):
    settings = Settings(db_path=tmp_path / "e2e.sqlite3", password_iterations=1_000, hashchain_iterations=20_000,
                        tick_interval_seconds=0.2, run_background_loop=True)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning", **tls))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.05)
    return server, thread, port


@pytest.fixture
def live_server(tmp_path):
    server, thread, port = start_server(tmp_path)
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


def accounts(base, verify=True):
    provider = httpx.Client(base_url=base, headers=CSRF, verify=verify)
    customer = httpx.Client(base_url=base, headers=CSRF, verify=verify)
    provider.post("/api/v1/auth/register", json={"email": "p@example.com", "password": PASSWORD, "display_name": "Provider"})
    customer.post("/api/v1/auth/register", json={"email": "c@example.com", "password": PASSWORD, "display_name": "Customer"})
    customer.post("/api/v1/wallet/demo-topup", json={"amount_usd": "10"})
    token = provider.post("/api/v1/provider/nodes", json={"kind": "agent", "name": "E2E node", "region": "nl-ams",
                                                          "hourly_usd": "0.50"}).json()["agent_token"]
    return provider, customer, token


def cpu():
    return GpuInfo(slot=0, device_type="cpu", vendor="Intel", model="Test CPU (4 threads)", vram_mb=0, detection="os")


def gtx():
    return GpuInfo(slot=0, vendor="NVIDIA", model="NVIDIA GeForce GTX 1050 Ti", vram_mb=4096, detection="nvidia-smi",
                   fingerprint="0f" * 16, cuda=True, compute_capability="6.1", cuda_driver_version=12020,
                   cuda_ordinal=0, display_active=False)


def start_agent(base, token, *, devices, policy, ca_file=None, options=None):
    agent = Agent(MarketplaceClient(base, token, ca_file=ca_file), poll_seconds=0.2, max_jobs=1, policy=policy,
                  options=options, detector=lambda: list(devices), cpu_detector=cpu, metrics=lambda: [])
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()
    return agent, thread


def submit_and_wait(customer, body):
    job = customer.post("/api/v1/jobs", json=body).json()["job"]
    return wait_for(lambda: (j := customer.get(f"/api/v1/jobs/{job['id']}").json()["job"])["status"]
                    in ("completed", "failed") and j)


def test_cpu_pipeline_check_over_http(live_server):
    provider, customer, token = accounts(live_server)
    agent, thread = start_agent(live_server, token, devices=[], policy=OfferPolicy(offer_cpu=True))
    wait_for(lambda: any(g["device_type"] == "cpu" and g["status"] == "available"
                         for g in customer.get("/api/v1/marketplace").json()["gpus"]))
    done = submit_and_wait(customer, {"name": "E2E check", "workload": "agent.hashchain", "min_vram_gb": 0,
                                      "est_minutes": 1, "max_budget_usd": "1.00", "region": "nl-ams"})
    assert done["status"] == "completed" and done["verified"] is True and done["executed_on"] == "cpu"
    assert done["evidence"]["level"] == "cpu_verified"
    assert [e["kind"] for e in done["events"]] == ["submitted", "assigned", "started", "completed"]
    thread.join(timeout=10)
    assert not thread.is_alive() and agent.jobs_done == 1
    assert provider.get("/api/v1/me").json()["balances"]["earnings_cents"] == done["provider_cents"]


def test_gpu_path_over_http_with_fake_driver(live_server, monkeypatch):
    monkeypatch.setattr(server_workloads, "MATMUL_N", 32)
    provider, customer, token = accounts(live_server)
    options = RunOptions(driver_factory=lambda: cuda.Driver(lib=FakeCuda()))
    agent, thread = start_agent(live_server, token, devices=[gtx()], policy=OfferPolicy(gpus=("0",)), options=options)
    wait_for(lambda: any(g["device_type"] == "gpu" and g["execution"] == "agent" and g["status"] == "available"
                         for g in customer.get("/api/v1/marketplace").json()["gpus"]))
    done = submit_and_wait(customer, {"name": "E2E GPU", "workload": "agent.cuda_matmul", "min_vram_gb": 1,
                                      "est_minutes": 1, "max_budget_usd": "1.00"})
    assert done["status"] == "completed" and done["executed_on"] == "gpu"
    assert done["evidence"]["level"] == "gpu_attested" and done["result"]["output"]["device"]["backend"] == "cuda-driver-ptx"
    thread.join(timeout=10)


def test_agent_rejects_gracefully_when_vram_is_short(live_server, monkeypatch):
    monkeypatch.setattr(server_workloads, "MATMUL_N", 32)
    _, customer, token = accounts(live_server)
    options = RunOptions(driver_factory=lambda: cuda.Driver(lib=FakeCuda(free_mb=5)))
    agent, thread = start_agent(live_server, token, devices=[gtx()], policy=OfferPolicy(gpus=("0",)), options=options)
    wait_for(lambda: any(g["execution"] == "agent" for g in customer.get("/api/v1/marketplace").json()["gpus"]))
    job = customer.post("/api/v1/jobs", json={"name": "Too big", "workload": "agent.cuda_matmul", "min_vram_gb": 1,
                                              "est_minutes": 1, "max_budget_usd": "1.00"}).json()["job"]
    j = wait_for(lambda: (x := customer.get(f"/api/v1/jobs/{job['id']}").json()["job"])["events"][-1]["kind"] == "rejected" and x)
    assert j["status"] == "pending" and j["attempts"] == 0 and "VRAM free" in j["events"][-1]["detail"]
    thread.join(timeout=10)


def test_agent_with_revoked_token_stops(live_server):
    agent = Agent(MarketplaceClient(live_server, "scn_" + "z" * 43), poll_seconds=0.1,
                  detector=lambda: [], cpu_detector=cpu, metrics=lambda: [])
    with pytest.raises(ApiError) as err:
        agent.run()
    assert err.value.status == 401


def test_agent_backs_off_when_server_is_unreachable():
    agent = Agent(MarketplaceClient(f"http://127.0.0.1:{free_port()}", "scn_x", timeout=1), poll_seconds=0.1,
                  detector=lambda: [], cpu_detector=cpu, metrics=lambda: [])
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()
    time.sleep(1.5)
    agent.stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


# --- TLS with a private CA (what docs/REMOTE_CONNECTIVITY.md sets up) -----------------------------

@pytest.fixture
def tls_files(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    out = tmp_path / "tls"
    subprocess.run(["sh", str(ROOT / "scripts" / "make-dev-tls.sh"), str(out), "localhost", "127.0.0.1"],
                   check=True, capture_output=True)
    return out


def test_agent_over_tls_with_private_ca(tmp_path, tls_files):
    server, thread, port = start_server(tmp_path, ssl_certfile=str(tls_files / "server.crt"),
                                        ssl_keyfile=str(tls_files / "server.key"))
    try:
        base = f"https://localhost:{port}"
        ca = str(tls_files / "ca.crt")
        _, customer, token = accounts(base, verify=ssl.create_default_context(cafile=ca))
        agent, agent_thread = start_agent(base, token, devices=[], policy=OfferPolicy(offer_cpu=True), ca_file=ca)
        done = submit_and_wait(customer, {"name": "TLS check", "workload": "agent.hashchain", "min_vram_gb": 0,
                                          "est_minutes": 1, "max_budget_usd": "1.00"})
        assert done["status"] == "completed" and done["verified"] is True
        agent_thread.join(timeout=10)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_agent_refuses_untrusted_certificate(tmp_path, tls_files):
    server, thread, port = start_server(tmp_path, ssl_certfile=str(tls_files / "server.crt"),
                                        ssl_keyfile=str(tls_files / "server.key"))
    try:
        client = MarketplaceClient(f"https://localhost:{port}", "scn_x")  # system trust store only
        with pytest.raises(Exception) as err:
            client.heartbeat({})
        assert getattr(err.value, "tls", False) is True
    finally:
        server.should_exit = True
        thread.join(timeout=10)
