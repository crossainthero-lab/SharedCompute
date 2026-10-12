import pytest
from fastapi.testclient import TestClient

from conftest import balances, get_job, ledger_invariants, submit, tick
from sharedcompute import workloads
from sharedcompute_agent import workloads as agent_workloads

GTX = {"slot": 0, "device_type": "gpu", "vendor": "NVIDIA", "model": "NVIDIA GeForce GTX 1050 Ti", "vram_mb": 4096,
       "vram_free_mb": 3900, "detection": "nvidia-smi", "fingerprint": "ab" * 16, "cuda": True,
       "compute_capability": "6.1", "cuda_driver_version": 12020, "driver_version": "535.183.01",
       "display_active": False, "offered": True, "workloads": ["agent.cuda_matmul"]}
CPU = {"slot": 0, "device_type": "cpu", "vendor": "Intel", "model": "Intel Core i7-9750H (12 threads)", "vram_mb": 0,
       "detection": "os", "offered": True, "workloads": ["agent.hashchain"]}
IGPU = {"slot": 1, "device_type": "gpu", "vendor": "Intel", "model": "Intel UHD Graphics 630", "vram_mb": 1536,
        "detection": "system_profiler", "integrated": True, "offered": False, "offer_note": "Not offered"}


def create_agent_node(client, **overrides):
    body = {"kind": "agent", "name": "Studio", "region": "us-west", "hourly_usd": "0.36", **overrides}
    r = client.post("/api/v1/provider/nodes", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def agent_client(app, token):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def hello(agent, devices=(GTX, CPU)):
    r = agent.post("/api/agent/v1/hello", json={"agent_version": "0.3.0", "platform": "Linux 6.8 x86_64",
                                                "gpus": list(devices)})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def node(app, bob):
    created = create_agent_node(bob)
    return {"id": created["node_id"], "token": created["agent_token"], "agent": agent_client(app, created["agent_token"])}


@pytest.fixture
def small_matmul(monkeypatch):
    monkeypatch.setattr(workloads, "MATMUL_N", 16)


def pipeline_job(client, **overrides):
    return submit(client, **{"name": "Pipeline check", "workload": "agent.hashchain", "min_vram_gb": 0,
                             "est_minutes": 2, "max_budget_usd": "1.00", **overrides})


def gpu_job(client, **overrides):
    return submit(client, **{"name": "Matmul", "workload": "agent.cuda_matmul", "min_vram_gb": 1,
                             "est_minutes": 2, "max_budget_usd": "1.00", **overrides})


def gpu_output(params, device=GTX, **device_overrides):
    """What a real agent with a working GPU would send (computed here on the CPU)."""
    a, b = agent_workloads.seeded_matrices(params["seed"], params["n"])
    c = agent_workloads.cpu_matmul(a, b, params["n"])
    claim = {"type": "gpu", "backend": "cuda-driver-ptx", "name": device["model"],
             "fingerprint": device["fingerprint"], "compute_capability": device["compute_capability"],
             **device_overrides}
    return {"device": claim, "kernel_ms": 0.05, "repeats": params["repeats"], "n": params["n"],
            **agent_workloads.commit(c, params["n"], params["nonce"], params["samples"])}


# --- Registration and opt-in --------------------------------------------------------------------

def test_token_is_returned_once_and_stored_hashed(app, bob, node):
    assert node["token"].startswith("scn_")
    assert node["token"] not in bob.get("/api/v1/provider/nodes").text
    with app.state.db.read() as conn:
        stored = conn.execute("SELECT token_hash FROM nodes WHERE id = ?", (node["id"],)).fetchone()[0]
    assert stored != node["token"] and len(stored) == 64


def test_agent_auth_required(app):
    assert TestClient(app).post("/api/agent/v1/hello", json={"agent_version": "x"}).status_code == 401
    assert agent_client(app, "scn_" + "x" * 43).post("/api/agent/v1/heartbeat", json={}).status_code == 401
    assert agent_client(app, "scs_" + "x" * 43).post("/api/agent/v1/jobs/claim").status_code == 401


def test_hello_registers_devices_with_capabilities(anon, bob, node):
    reply = hello(node["agent"], (GTX, CPU, IGPU))
    assert reply["capabilities"] == ["agent.cuda_matmul", "agent.hashchain"]
    by_model = {d["model"]: d for d in reply["gpus"]}
    assert by_model[GTX["model"]]["workloads"] == ["agent.cuda_matmul"] and by_model[GTX["model"]]["enabled"]
    assert by_model[CPU["model"]]["workloads"] == ["agent.hashchain"]
    assert by_model[IGPU["model"]]["offered"] is False and by_model[IGPU["model"]]["enabled"] is False
    listing = {g["model"]: g for g in anon.get("/api/v1/marketplace").json()["gpus"] if g["execution"] == "agent"}
    assert set(listing) == {GTX["model"], CPU["model"]}  # not-offered devices stay private
    assert listing[GTX["model"]]["compute_capability"] == "6.1" and listing[CPU["model"]]["device_type"] == "cpu"
    mine = bob.get("/api/v1/provider/nodes").json()["nodes"][0]["gpus"]
    igpu = next(d for d in mine if d["model"] == IGPU["model"])
    assert igpu["status"] == "not_offered" and igpu["offer_note"] == "Not offered"


def test_device_not_offered_is_never_scheduled(app, alice, node):
    hello(node["agent"], (dict(GTX, offered=False), dict(CPU, offered=False)))
    assert gpu_job(alice)["status"] == "pending"
    assert pipeline_job(alice)["status"] == "pending"
    assert node["agent"].post("/api/agent/v1/jobs/claim").json()["job"] is None


def test_withdrawing_a_device_requeues_its_job(app, alice, node):
    hello(node["agent"])
    job = pipeline_job(alice)
    node["agent"].post("/api/agent/v1/jobs/claim")
    hello(node["agent"], (GTX, dict(CPU, offered=False)))
    j = get_job(alice, job["id"])
    assert j["status"] == "pending" and "withdrew" in j["events"][-2]["detail"]


def test_server_rechecks_compatibility_whatever_the_agent_claims(anon, alice, node):
    fermi = dict(GTX, compute_capability="2.1", fingerprint="cd" * 16, slot=0)
    reply = hello(node["agent"], (fermi,))
    (dev,) = reply["gpus"]
    assert dev["workloads"] == [] and "compute capability 2.1 is below 3.0" in dev["rejected_workloads"]["agent.cuda_matmul"][0]
    job = gpu_job(alice)
    assert job["status"] == "pending"
    q = anon.post("/api/v1/quotes", json={"workload": "agent.cuda_matmul", "min_vram_gb": 1, "est_minutes": 2,
                                          "max_budget_usd": "1"}).json()
    assert q["outcome"] == "no_capacity" and "compute capability 2.1 is below 3.0" in q["reason"]


@pytest.mark.parametrize("overrides,reason", [
    ({"cuda_driver_version": 5000}, "needs 5.5 or newer"),
    ({"cuda": False}, "working CUDA driver"),
    ({"vram_mb": 32}, "below 64 MB"),
])
def test_old_hardware_is_rejected_gracefully_at_match_time(anon, alice, node, overrides, reason):
    hello(node["agent"], (dict(GTX, **overrides),))
    assert gpu_job(alice)["status"] == "pending"
    q = anon.post("/api/v1/quotes", json={"workload": "agent.cuda_matmul", "min_vram_gb": 0, "est_minutes": 2,
                                          "max_budget_usd": "1"}).json()
    assert reason in q["reason"]


def test_customer_minimum_compute_capability(anon, alice, node):
    hello(node["agent"])
    job = gpu_job(alice, min_compute_capability="7.5")
    assert job["status"] == "pending" and job["min_compute_capability"] == "7.5"
    q = anon.post("/api/v1/quotes", json={"workload": "agent.cuda_matmul", "min_vram_gb": 1, "est_minutes": 2,
                                          "max_budget_usd": "1", "min_compute_capability": "7.5"}).json()
    assert "compute capability 7.5" in q["reason"]
    assert gpu_job(alice, min_compute_capability="6.0")["status"] == "assigned"
    r = alice.post("/api/v1/jobs", json={"name": "x y", "workload": "agent.cuda_matmul", "min_vram_gb": 1,
                                         "est_minutes": 1, "max_budget_usd": "1", "min_compute_capability": "latest"})
    assert r.status_code == 422 and r.json()["error"]["field"] == "min_compute_capability"


def test_hello_validation(node):
    for bad in (dict(GTX, detection="made-up"), dict(GTX, fingerprint="NOT-HEX"), dict(GTX, device_type="tpu"),
                dict(GTX, compute_capability="six")):
        r = node["agent"].post("/api/agent/v1/hello", json={"agent_version": "0.3.0", "gpus": [bad]})
        assert r.status_code == 422, bad
    reply = hello(node["agent"], (dict(GTX, workloads=["agent.cuda_matmul", "agent.hashchain", "sim.image", "rm -rf /"]),))
    assert reply["gpus"][0]["workloads"] == ["agent.cuda_matmul"]  # only GPU workloads stick to a GPU


def test_hello_is_idempotent_and_tracks_removed_devices(bob, node):
    hello(node["agent"], (GTX, CPU))
    hello(node["agent"], (GTX, CPU))
    assert len(bob.get("/api/v1/provider/nodes").json()["nodes"][0]["gpus"]) == 2
    hello(node["agent"], (CPU,))
    assert [d["device_type"] for d in bob.get("/api/v1/provider/nodes").json()["nodes"][0]["gpus"]] == ["cpu"]


def test_offline_until_first_hello_then_heartbeat_expiry(app, anon, node, clock):
    assert all(g["execution"] != "agent" for g in anon.get("/api/v1/marketplace").json()["gpus"])
    hello(node["agent"], (GTX,))
    clock.advance(app.state.settings.node_ttl_seconds + 1)
    g = next(g for g in anon.get("/api/v1/marketplace").json()["gpus"] if g["execution"] == "agent")
    assert g["status"] == "offline"
    node["agent"].post("/api/agent/v1/heartbeat", json={"gpu_metrics": [{"slot": 0, "util_percent": 12.5,
                                                                           "mem_used_mb": 900, "temperature_c": 51}]})
    g = next(g for g in anon.get("/api/v1/marketplace").json()["gpus"] if g["execution"] == "agent")
    assert g["status"] == "available" and g["metrics"] == {"util_percent": 12.5, "mem_used_mb": 900, "temperature_c": 51.0}


# --- CPU pipeline check --------------------------------------------------------------------------

def test_cpu_pipeline_check_is_verified_and_metered(app, alice, bob, node, clock):
    hello(node["agent"])
    job = pipeline_job(alice)
    assert job["status"] == "assigned" and job["gpu"]["device_type"] == "cpu"
    spec = node["agent"].post("/api/agent/v1/jobs/claim").json()["job"]
    assert spec["device"] == {"type": "cpu", "slot": 0, "fingerprint": None} and spec["time_limit_seconds"] == 120
    assert node["agent"].post("/api/agent/v1/jobs/claim").json()["job"] is None
    output = agent_workloads.hashchain(spec["params"], lambda: False)
    clock.advance(30)
    r = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/complete", json={"output": output, "runtime_seconds": 999})
    assert r.json()["status"] == "completed" and r.json()["evidence"] == "cpu_verified"
    done = get_job(alice, job["id"])
    assert done["executed_on"] == "cpu" and done["billed_seconds"] == 30 and done["cost_cents"] == 1
    assert done["result"]["agent_reported_runtime_seconds"] == 999  # recorded, not trusted
    assert balances(bob)["earnings_cents"] == 1
    assert node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/complete", json={"output": output}).status_code == 409
    ledger_invariants(app)


def test_wrong_digest_fails_and_is_not_billed(app, alice, bob, node):
    hello(node["agent"])
    job = pipeline_job(alice)
    node["agent"].post("/api/agent/v1/jobs/claim")
    r = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/complete",
                           json={"output": {"digest": "00" * 32, "device": {"type": "cpu"}}})
    assert r.json()["status"] == "failed"
    failed = get_job(alice, job["id"])
    assert failed["verified"] is False and failed["cost_cents"] is None and failed["evidence"]["level"] == "failed_verification"
    assert balances(alice)["held_cents"] == 0 and balances(bob)["earnings_cents"] == 0
    ledger_invariants(app)


# --- GPU matmul and execution evidence -----------------------------------------------------------

def run_gpu_job(alice, node, clock, output_fn, seconds=5):
    hello(node["agent"])
    job = gpu_job(alice)
    spec = node["agent"].post("/api/agent/v1/jobs/claim").json()["job"]
    assert spec["device"] == {"type": "gpu", "slot": 0, "fingerprint": GTX["fingerprint"]}
    clock.advance(seconds)
    r = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/complete", json={"output": output_fn(spec["params"])})
    return r.json(), get_job(alice, job["id"])


def test_gpu_job_completes_with_attested_evidence(app, alice, bob, node, clock, small_matmul):
    reply, job = run_gpu_job(alice, node, clock, gpu_output)
    assert reply["status"] == "completed" and reply["evidence"] == "gpu_attested"
    assert job["executed_on"] == "gpu" and job["verified"] is True
    ev = job["evidence"]
    assert ev["device_identity"] == {"fingerprint_matches": True, "compute_capability_matches": True}
    assert ev["timing"]["consistent"] is True and "not cryptographically proven" in ev["note"]
    assert "samples" not in job["result"]["output"]  # bulky revealed rows are not stored
    assert balances(bob)["earnings_cents"] == job["provider_cents"]
    ledger_invariants(app)


def test_cpu_fallback_result_is_recorded_but_not_billed(app, alice, bob, node, clock, small_matmul):
    def fallback(params):
        out = gpu_output(params)
        out["device"] = {"type": "cpu", "backend": "python-cpu-fallback", "fallback_reason": "CUDA failed"}
        return out
    reply, job = run_gpu_job(alice, node, clock, fallback)
    assert reply["status"] == "failed" and reply["evidence"] == "cpu_fallback"
    assert job["executed_on"] == "cpu" and job["verified"] is True and job["cost_cents"] is None
    assert "requires a GPU" in job["failure_reason"]
    assert balances(bob)["earnings_cents"] == 0 and balances(alice)["held_cents"] == 0
    ledger_invariants(app)


@pytest.mark.parametrize("tamper,expect", [
    (lambda o: o.update(checksum=o["checksum"] + 1), "Checksum"),
    (lambda o: o["samples"][0]["values"].__setitem__(0, o["samples"][0]["values"][0] + 1), "not part of the committed"),
    (lambda o: o["samples"].reverse(), "challenge"),
    (lambda o: o.update(merkle_root="00" * 32), "challenge"),
    (lambda o: o["samples"][0].update(proof=[]), "not part of the committed"),
])
def test_tampered_gpu_results_fail_verification(app, alice, node, clock, small_matmul, tamper, expect):
    def bad(params):
        out = gpu_output(params)
        tamper(out)
        return out
    reply, job = run_gpu_job(alice, node, clock, bad)
    assert reply["status"] == "failed" and job["cost_cents"] is None and expect in job["failure_reason"]
    ledger_invariants(app)


def test_consistent_wrong_rows_are_caught(app, alice, node, clock, small_matmul):
    def forged(params):
        # A cheater commits to a fake matrix whose checksum is right but whose rows are not the product.
        n = params["n"]
        c = [0] * (n * n)
        c[0] = workloads.matmul_checksum(params["seed"], n)
        return {"device": {"type": "gpu", "fingerprint": GTX["fingerprint"], "compute_capability": "6.1"},
                "kernel_ms": 0.05, "repeats": params["repeats"], **agent_workloads.commit(c, n, params["nonce"], params["samples"])}
    reply, job = run_gpu_job(alice, node, clock, forged)
    assert reply["status"] == "failed" and "is wrong" in job["failure_reason"]


@pytest.mark.parametrize("claim", [{"fingerprint": "ff" * 16}, {"compute_capability": "8.9"}])
def test_device_identity_mismatch_is_not_billed(app, alice, node, clock, small_matmul, claim):
    reply, job = run_gpu_job(alice, node, clock, lambda p: gpu_output(p, **claim))
    assert reply["evidence"] == "inconsistent" and job["status"] == "failed" and job["cost_cents"] is None


def test_impossible_timing_is_not_billed(app, alice, node, clock, small_matmul):
    def slow(params):
        out = gpu_output(params)
        out["kernel_ms"] = 60_000  # claims 20 x 60 s of kernel time inside a 5 s window
        return out
    reply, job = run_gpu_job(alice, node, clock, slow)
    assert reply["evidence"] == "inconsistent" and job["evidence"]["timing"]["consistent"] is False


def test_graceful_rejection_requeues_without_costing_an_attempt(app, alice, bob, node, small_matmul):
    hello(node["agent"])
    job = gpu_job(alice)
    node["agent"].post("/api/agent/v1/jobs/claim")
    r = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/fail",
                           json={"reason": "only 20 MB of VRAM free", "retryable": True, "code": "incompatible"})
    assert r.json() == {"status": "pending"}
    j = get_job(alice, job["id"])
    assert j["status"] == "pending" and j["attempts"] == 0  # not re-matched to the same device
    assert j["events"][-1]["kind"] == "rejected" and "VRAM" in j["events"][-1]["detail"]
    gpu = next(g for g in bob.get("/api/v1/provider/nodes").json()["nodes"][0]["gpus"] if g["device_type"] == "gpu")
    assert "agent.cuda_matmul" not in gpu["workloads"]
    track = next(g for g in alice.get("/api/v1/marketplace").json()["gpus"] if g["id"] == gpu["id"])["reliability"]
    assert track["issues"] == 0  # declining up front is not a strike against the provider
    hello(node["agent"])  # agent re-advertises (e.g. VRAM freed up)
    assert get_job(alice, job["id"])["status"] == "assigned"
    ledger_invariants(app)


def test_agent_reported_failures(app, alice, node):
    hello(node["agent"])
    job = pipeline_job(alice)
    node["agent"].post("/api/agent/v1/jobs/claim")
    r = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/fail", json={"reason": "OOM", "retryable": True})
    assert r.json() == {"status": "pending"}
    assert get_job(alice, job["id"])["status"] == "assigned"
    node["agent"].post("/api/agent/v1/jobs/claim")
    r = node["agent"].post(f"/api/agent/v1/jobs/{job['id']}/fail", json={"reason": "Bad input", "retryable": False})
    assert r.json() == {"status": "failed"}
    ledger_invariants(app)


# --- Liveness, leases and control ----------------------------------------------------------------

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
    for _ in range(int(app.state.settings.claim_timeout_seconds // 10) + 1):
        clock.advance(10)
        node["agent"].post("/api/agent/v1/heartbeat", json={})
    tick(app)
    assert any(e["kind"] == "interrupted" and "did not claim" in e["detail"] for e in get_job(alice, job["id"])["events"])


def test_time_limit_enforced_and_agent_told_to_stop(app, alice, node, clock):
    hello(node["agent"])
    job = pipeline_job(alice, est_minutes=1)
    node["agent"].post("/api/agent/v1/jobs/claim")
    for _ in range(13):
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
    assert pipeline_job(alice)["status"] == "pending"
    assert node["agent"].post("/api/agent/v1/jobs/claim").json()["job"] is None


def test_one_job_at_a_time_per_agent_node(app, alice, node):
    hello(node["agent"], (GTX, dict(GTX, slot=1, fingerprint="cd" * 16)))
    first, second = gpu_job(alice), gpu_job(alice)
    assert (first["status"], second["status"]) == ("assigned", "pending")


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
    hello(intruder, (dict(GTX, fingerprint="12" * 16),))
    assert intruder.post(f"/api/agent/v1/jobs/{job['id']}/complete", json={"output": {}}).status_code == 404
    assert intruder.post(f"/api/agent/v1/jobs/{job['id']}/fail", json={}).status_code == 404
