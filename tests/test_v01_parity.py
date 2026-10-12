"""The v0.1 draft's test scenarios (tests/test_marketplace.py at the baseline commit), ported to the
v0.2 API. Same behaviour, plus the funded-hold rules the draft did not have."""
from conftest import balances, get_job, submit, tick


def test_seed_inventory(anon):
    data = anon.get("/api/v1/marketplace").json()
    assert data["summary"]["listed"] == 4
    assert data["summary"]["available"] == 4
    assert all(g["execution"] == "simulated" for g in data["gpus"])


def test_cheapest_gpu_selected_and_paid_only_on_completion(app, alice):
    job = submit(alice, min_vram_gb=24)
    assert job["gpu"]["model"] == "GeForce RTX 3090"
    assert job["status"] == "assigned"
    tick(app)
    assert get_job(alice, job["id"])["status"] == "running"
    assert job["quote_cents"] == 44
    assert get_job(alice, job["id"])["cost_cents"] is None  # nothing charged yet
    assert alice.post(f"/api/v1/jobs/{job['id']}/demo/complete").status_code == 200
    done = get_job(alice, job["id"])
    assert (done["cost_cents"], done["provider_cents"], done["platform_cents"]) == (44, 40, 4)
    again = alice.post(f"/api/v1/jobs/{job['id']}/demo/complete")
    assert again.status_code == 409
    assert get_job(alice, job["id"])["cost_cents"] == 44
    assert balances(alice)["available_cents"] == 10_000 - 44


def test_queue_then_activate_when_affordable_provider_joins(alice, bob):
    job = submit(alice, min_vram_gb=48, max_budget_usd="0.30")
    assert job["status"] == "pending"
    bob.post("/api/v1/provider/nodes", json={"kind": "simulated", "name": "Founder PC", "region": "au-syd",
                                            "gpu_model": "RTX Pro 48GB", "vram_gb": 48, "hourly_usd": "0.25"})
    job = get_job(alice, job["id"])
    assert job["status"] == "assigned"
    assert job["gpu"]["node_name"] == "Founder PC"


def test_offline_requeues_region_bound_job(app, alice):
    job = submit(alice, region="de-fra", min_vram_gb=24)
    assert job["gpu"]["model"] == "GeForce RTX 4090"
    with app.state.db.write() as conn:  # the sample network's owner pauses the node
        node_id = conn.execute("SELECT node_id FROM jobs WHERE id = ?", (job["id"],)).fetchone()[0]
        from sharedcompute.services import nodes as node_svc
        owner = conn.execute("SELECT owner_id FROM nodes WHERE id = ?", (node_id,)).fetchone()[0]
        node_svc.update_node(conn, app.state.settings, owner, node_id, {"enabled": False})
    assert get_job(alice, job["id"])["status"] == "pending"
    with app.state.db.write() as conn:
        node_svc.update_node(conn, app.state.settings, owner, node_id, {"enabled": True})
    assert get_job(alice, job["id"])["status"] == "pending"  # re-matched by the next scheduler pass
    tick(app)
    assert get_job(alice, job["id"])["status"] == "assigned"
    tick(app)
    assert get_job(alice, job["id"])["status"] == "running"


def test_busy_gpu_cannot_take_second_job(app, alice):
    a = submit(alice, region="au-syd")
    b = submit(alice, region="au-syd")
    assert (a["status"], b["status"]) == ("assigned", "pending")
    alice.post(f"/api/v1/jobs/{a['id']}/demo/complete")
    assert get_job(alice, b["id"])["status"] == "assigned"


def test_invalid_requests(alice):
    assert alice.post("/api/v1/jobs", json={"name": "x", "workload": "sim.image", "min_vram_gb": 12,
                                            "est_minutes": 60, "max_budget_usd": "-1"}).status_code == 422
    assert alice.post("/api/v1/jobs", json={"name": "Shell", "workload": "execute_arbitrary_shell",
                                            "min_vram_gb": 12, "est_minutes": 60,
                                            "max_budget_usd": "1"}).status_code == 422
    assert alice.post("/api/v1/jobs", json={"name": "Zero", "workload": "sim.image", "min_vram_gb": 12,
                                            "est_minutes": 0, "max_budget_usd": "1"}).status_code == 422
    assert alice.post("/api/v1/provider/nodes", json={"kind": "simulated", "name": "<script>alert(1)</script>",
                                                      "region": "au-syd", "gpu_model": "RTX", "vram_gb": 0,
                                                      "hourly_usd": "1"}).status_code == 422
