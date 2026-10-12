import pytest
from fastapi.testclient import TestClient

from conftest import balances, get_job, ledger_invariants, submit, tick
from sharedcompute import workloads
from sharedcompute_agent import workloads as agent_workloads

RTX = {"slot": 0, "vendor": "NVIDIA", "model": "NVIDIA GeForce RTX 4090", "vram_mb": 24564, "detection": "nvml",
       "fingerprint": "ab" * 16, "cuda": True, "compute_capability": "8.9", "driver_version": "550.54.15"}
IGPU = {"slot": 1, "vendor": "Intel", "model": "Intel UHD Graphics 630", "vram_mb": 1536,
        "detection": "system_profiler", "integrated": True}


def create_agent_node(client, **overrides):
    body = {"kind": "agent", "name": "Studio", "region": "us-west", "hourly_usd": "0.36", **overrides}
    r = client.post("/api/v1/provider/nodes", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def agent_client(app, token):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def hello(agent, gpus=(RTX,), caps=("agent.hashchain",)):
    r = agent.post("/api/agent/v1/hello", json={"agent_version": "0.2.0", "platform": "Linux 6.8 x86_64",
                                                "capabilities": list(caps), "gpus": list(gpus)})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def node(app, bob):
    created = create_agent_node(bob)
    agent = agent_client(app, created["agent_token"])
    return {"id": created["node_id"], "token": created["agent_token"], "agent": agent}


def pipeline_job(client, **overrides):
    return submit(client, **{"name": "Pipeline check", "workload": "agent.hashchain", "min_vram_gb": 1,
                             "est_minutes": 2, "max_budget_usd": "1.00", **overrides})


def test_token_is_returned_once_and_stored_hashed(app, bob, node):
    assert node["token"].startswith("scn_")
    assert node["token"] not in bob.get("/api/v1/provider/nodes").text  # never echoed back
    with app.state.db.read() as conn:
        stored = conn.execute("SELECT token_hash FROM nodes WHERE id = ?", (node["id"],)).fetchone()[0]
    assert stored != node["token"] and len(stored) == 64


def test_agent_auth_required(app):
    assert TestClient(app).post("/api/agent/v1/hello", json={"agent_version": "x"}).status_code == 401
    bogus = agent_client(app, "scn_" + "x" * 43)
    assert bogus.post("/api/agent/v1/heartbeat", json={}).status_code == 401
    session_like = agent_client(app, "scs_" + "x" * 43)
    assert session_like.post("/api/agent/v1/jobs/claim").status_code == 401


def test_hello_registers_detected_gpus(app, anon, bob, node):
    reply = hello(node["agent"], gpus=(RTX, IGPU))
    assert reply["capabilities"] == ["agent.hashchain"]
    assert [(g["model"], g["enabled"], g["hourly_cents"]) for g in reply["gpus"]] == [
        ("NVIDIA GeForce RTX 4090", True, 36), ("Intel UHD Graphics 630", False, 36)]  # integrated starts paused
    listing = [g for g in anon.get("/api/v1/marketplace").json()["gpus"] if g["execution"] == "agent"]
    rtx = next(g for g in listing if g["cuda"])
    assert rtx["status"] == "available" and rtx["detection"] == "nvml" and rtx["vram_gb"] == 24.0
    assert next(g for g in listing if not g["cuda"])["status"] == "paused"
    nodes = bob.get("/api/v1/provider/nodes").json()["nodes"]
    assert nodes[0]["online"] and nodes[0]["agent_version"] == "0.2.0"


def test_hello_is_idempotent_and_tracks_removed_gpus(app, bob, node):
    hello(node["agent"], gpus=(RTX, IGPU))
    hello(node["agent"], gpus=(RTX, IGPU))
    gpus = bob.get("/api/v1/provider/nodes").json()["nodes"][0]["gpus"]
    assert len(gpus) == 2
    hello(node["agent"], gpus=(RTX,))
    assert len(bob.get("/api/v1/provider/nodes").json()["nodes"][0]["gpus"]) == 1


def test_hello_rejects_unknown_capabilities_and_bad_reports(node):
    reply = hello(node["agent"], caps=("agent.hashchain", "sim.image", "rm -rf /"))
    assert reply["capabilities"] == ["agent.hashchain"]
    bad = dict(RTX, detection="made-up")
    assert node["agent"].post("/api/agent/v1/hello", json={"agent_version": "0.2.0", "gpus": [bad]}).status_code == 422
    bad = dict(RTX, fingerprint="NOT-HEX")
    assert node["agent"].post("/api/agent/v1/hello", json={"agent_version": "0.2.0", "gpus": [bad]}).status_code == 422


def test_offline_until_first_hello_then_heartbeat_expiry(app, anon, node, clock):
    assert all(g["execution"] != "agent" for g in anon.get("/api/v1/marketplace").json()["gpus"])
    hello(node["agent"])
    clock.advance(app.state.settings.node_ttl_seconds + 1)
    agent_gpu = next(g for g in anon.get("/api/v1/marketplace").json()["gpus"] if g["execution"] == "agent")
    assert agent_gpu["status"] == "offline"
    node["agent"].post("/api/agent/v1/heartbeat", json={"gpu_metrics": [{"slot": 0, "util_percent": 12.5,
                                                                           "mem_used_mb": 900, "temperature_c": 51}]})
    agent_gpu = next(g for g in anon.get("/api/v1/marketplace").json()["gpus"] if g["execution"] == "agent")
    assert agent_gpu["status"] == "available"
    assert agent_gpu["metrics"] == {"util_percent": 12.5, "mem_used_mb": 900, "temperature_c": 51.0}


def test_full_agent_job_lifecycle_is_verified_and_metered(app, alice, bob, node, clock):
    hello(node["agent"])
    job = pipeline_job(alice)
    assert job["status"] == "assigned" and job["gpu"]["node_kind"] == "agent"

    spec = node["agent"].post("/api/agent/v1/jobs/claim").json()["job"]
    assert spec["id"] == job["id"] and spec["workload"] == "agent.hashchain"
    assert spec["time_limit_seconds"] == 120 and spec["gpu_slot"] == 0
    assert node["agent"].post("/api/agent/v1/jobs/claim").json()["job"] is None
    assert get_job(alice, job["id"])["status"] == "running"

    output = agent_workloads.hashchain(spec["params"], lambda: False)
    clock.advance(30)  # server-measured runtime is what gets billed
    r = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/complete", json={"output": output, "runtime_seconds": 999})
    assert r.json() == {"status": "completed", "verified": True, "detail": "Digest matches server recomputation"}

    done = get_job(alice, job["id"])
    assert done["verified"] is True and done["billed_seconds"] == 30
    assert done["cost_cents"] == 1  # 30 s at $0.36/h = 0.3 cents -> one-cent minimum
    assert done["result"]["agent_reported_runtime_seconds"] == 999  # recorded, not trusted
    assert balances(bob)["earnings_cents"] == 1
    again = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/complete", json={"output": output})
    assert again.status_code == 409
    ledger_invariants(app)


def test_long_agent_job_cost_is_metered_and_capped_at_quote(app, alice, node, clock):
    hello(node["agent"])
    job = pipeline_job(alice, est_minutes=60, max_budget_usd="5.00")  # quote: 60 min at $0.36 = $0.36
    spec = node["agent"].post("/api/agent/v1/jobs/claim").json()["job"]
    clock.advance(1800)
    node["agent"].post("/api/agent/v1/heartbeat", json={"running_job_ids": [job["id"]]})
    node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/complete",
                       json={"output": agent_workloads.hashchain(spec["params"], lambda: False)})
    assert get_job(alice, job["id"])["cost_cents"] == 18  # half an hour at $0.36
    ledger_invariants(app)


def test_wrong_result_fails_verification_and_is_not_billed(app, alice, bob, node):
    hello(node["agent"])
    job = pipeline_job(alice)
    node["agent"].post("/api/agent/v1/jobs/claim")
    r = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/complete", json={"output": {"digest": "00" * 32}})
    assert r.json()["status"] == "failed" and r.json()["verified"] is False
    failed = get_job(alice, job["id"])
    assert failed["status"] == "failed" and failed["verified"] is False and failed["cost_cents"] is None
    assert balances(alice)["held_cents"] == 0 and balances(bob)["earnings_cents"] == 0
    ledger_invariants(app)


def test_agent_reported_failures(app, alice, node):
    hello(node["agent"])
    job = pipeline_job(alice)
    node["agent"].post("/api/agent/v1/jobs/claim")
    r = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/fail", json={"reason": "CUDA OOM", "retryable": True})
    assert r.json() == {"status": "pending"}
    assert get_job(alice, job["id"])["status"] == "assigned"  # re-matched to the same idle node
    node["agent"].post("/api/agent/v1/jobs/claim")
    r = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/fail", json={"reason": "Bad input", "retryable": False})
    assert r.json() == {"status": "failed"}
    assert get_job(alice, job["id"])["failure_reason"] == "Agent: Bad input"
    ledger_invariants(app)


def test_silent_node_jobs_are_requeued(app, alice, node, clock):
    hello(node["agent"])
    job = pipeline_job(alice)
    node["agent"].post("/api/agent/v1/jobs/claim")
    clock.advance(app.state.settings.node_ttl_seconds + 1)
    tick(app)
    j = get_job(alice, job["id"])
    assert j["status"] == "pending" and j["attempts"] == 1
    assert "stopped sending heartbeats" in [e for e in j["events"] if e["kind"] == "interrupted"][0]["detail"]


def test_unclaimed_assignment_times_out(app, alice, node, clock):
    hello(node["agent"])
    job = pipeline_job(alice)
    for _ in range(int(app.state.settings.claim_timeout_seconds // 10) + 1):  # keep the node alive
        clock.advance(10)
        node["agent"].post("/api/agent/v1/heartbeat", json={})
    tick(app)
    events = get_job(alice, job["id"])["events"]
    assert any(e["kind"] == "interrupted" and "did not claim" in e["detail"] for e in events)


def test_time_limit_enforced_and_agent_told_to_stop(app, alice, node, clock):
    hello(node["agent"])
    job = pipeline_job(alice, est_minutes=1)
    node["agent"].post("/api/agent/v1/jobs/claim")
    for _ in range(13):  # 130 s > 60 s limit + 60 s grace, heartbeating throughout
        clock.advance(10)
        node["agent"].post("/api/agent/v1/heartbeat", json={"running_job_ids": [job["id"]]})
    tick(app)
    assert get_job(alice, job["id"])["failure_reason"] == "Exceeded the job's time limit"
    reply = node["agent"].post("/api/agent/v1/heartbeat", json={"running_job_ids": [job["id"]]}).json()
    assert reply["cancel_job_ids"] == [job["id"]]
    ledger_invariants(app)


def test_customer_cancel_reaches_agent_via_heartbeat(app, alice, node):
    hello(node["agent"])
    job = pipeline_job(alice)
    node["agent"].post("/api/agent/v1/jobs/claim")
    alice.post(f"/api/v1/jobs/{job['id']}/cancel")
    reply = node["agent"].post("/api/agent/v1/heartbeat", json={"running_job_ids": [job["id"]]}).json()
    assert reply["cancel_job_ids"] == [job["id"]]
    assert node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/complete", json={"output": {}}).status_code == 409


def test_paused_node_does_not_receive_work(app, alice, bob, node):
    hello(node["agent"])
    bob.patch(f"/api/v1/provider/nodes/{node['id']}", json={"enabled": False})
    job = pipeline_job(alice)
    assert job["status"] == "pending"
    assert node["agent"].post("/api/agent/v1/jobs/claim").json()["job"] is None


def test_one_job_at_a_time_per_agent_node(app, alice, node):
    hello(node["agent"], gpus=(RTX, dict(RTX, slot=1, fingerprint="cd" * 16)))
    first, second = pipeline_job(alice), pipeline_job(alice)
    assert (first["status"], second["status"]) == ("assigned", "pending")


def test_matmul_requires_cuda_capability(app, alice, bob):
    no_torch = create_agent_node(bob, name="No torch")
    agent = agent_client(app, no_torch["agent_token"])
    hello(agent, gpus=(RTX,), caps=("agent.hashchain",))
    job = submit(alice, name="Bench", workload="agent.matmul", min_vram_gb=8, est_minutes=5, max_budget_usd="1")
    assert job["status"] == "pending"
    mac = create_agent_node(bob, name="Mac")
    hello(agent_client(app, mac["agent_token"]), gpus=(dict(IGPU, integrated=False),),
          caps=("agent.hashchain", "agent.matmul"))
    assert get_job(alice, job["id"])["status"] == "pending"  # advertised, but the GPU is not CUDA
    cuda = create_agent_node(bob, name="CUDA box")
    cuda_agent = agent_client(app, cuda["agent_token"])
    hello(cuda_agent, gpus=(dict(RTX, fingerprint="ef" * 16),), caps=("agent.hashchain", "agent.matmul"))
    assert get_job(alice, job["id"])["status"] == "assigned"
    spec = cuda_agent.post("/api/agent/v1/jobs/claim").json()["job"]
    checksum = workloads.matmul_checksum(spec["params"]["seed"], spec["params"]["n"])
    r = cuda_agent.post(f"/api/agent/v1/jobs/{job['id']}/complete",
                        json={"output": {"checksum": checksum, "device": "test", "tflops": 1.0}})
    assert r.json()["verified"] is True


def test_rotating_token_revokes_the_old_one(app, bob, node):
    hello(node["agent"])
    new = bob.post(f"/api/v1/provider/nodes/{node['id']}/token").json()["agent_token"]
    assert node["agent"].post("/api/agent/v1/heartbeat", json={}).status_code == 401
    assert agent_client(app, new).post("/api/agent/v1/heartbeat", json={}).status_code == 200


def test_retired_node_token_stops_working(app, bob, node):
    hello(node["agent"])
    assert bob.delete(f"/api/v1/provider/nodes/{node['id']}").status_code == 200
    assert node["agent"].post("/api/agent/v1/heartbeat", json={}).status_code == 401


def test_agent_cannot_touch_other_nodes_jobs(app, alice, bob, node):
    hello(node["agent"])
    job = pipeline_job(alice)
    other = create_agent_node(bob, name="Other")
    intruder = agent_client(app, other["agent_token"])
    hello(intruder, gpus=(dict(RTX, fingerprint="12" * 16),))
    assert intruder.post(f"/api/agent/v1/jobs/{job['id']}/complete", json={"output": {}}).status_code == 404
    assert intruder.post(f"/api/agent/v1/jobs/{job['id']}/fail", json={}).status_code == 404
