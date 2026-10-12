from conftest import submit, tick


def listing(client):
    return client.get("/api/v1/marketplace").json()


def sim_node(client, **overrides):
    body = {"kind": "simulated", "name": "Founder PC", "region": "au-syd", "gpu_model": "GeForce RTX 3060",
            "vram_gb": 12, "hourly_usd": "0.30"}
    body.update(overrides)
    return client.post("/api/v1/provider/nodes", json=body)


def test_sample_network_is_seeded_and_labelled_simulated(anon):
    data = listing(anon)
    assert data["summary"]["listed"] == 4
    assert data["summary"]["available"] == 4
    assert data["summary"]["agent_connected"] == 0
    assert data["summary"]["lowest_available_cents"] == 24
    for g in data["gpus"]:
        assert g["execution"] == "simulated"
        assert g["detection"] == "self_reported"
        assert g["reliability"]["score"] is None  # no fabricated track record
        assert "sim.image" in g["workloads"] and "agent.hashchain" not in g["workloads"]
    assert [g["hourly_cents"] for g in data["gpus"]] == sorted(g["hourly_cents"] for g in data["gpus"])


def test_marketplace_is_public_but_hides_paused_status_correctly(anon, alice):
    assert anon.get("/api/v1/marketplace").status_code == 200
    node_id = sim_node(alice).json()["node_id"]
    gpu = next(g for g in listing(anon)["gpus"] if g["node"]["id"] == node_id)
    assert gpu["status"] == "available" and gpu["provider_name"] == "Alice" and gpu["vendor"] == "NVIDIA"
    alice.patch(f"/api/v1/provider/nodes/{node_id}", json={"enabled": False})
    gpu = next(g for g in listing(anon)["gpus"] if g["node"]["id"] == node_id)
    assert gpu["status"] == "paused"
    assert listing(anon)["summary"]["available"] == 4


def test_provider_registration_validation(alice):
    cases = [({"vram_gb": 0}, "vram_gb"), ({"region": "Mars"}, "region"), ({"hourly_usd": "-1"}, "hourly_usd"),
             ({"hourly_usd": "100.01"}, "hourly_usd"), ({"hourly_usd": "0.001"}, "hourly_usd"),
             ({"name": "<script>alert(1)</script>"}, "name"), ({"kind": "mainframe"}, "kind"),
             ({"gpu_model": ""}, "gpu_model")]
    for overrides, field in cases:
        r = sim_node(alice, **overrides)
        assert r.status_code == 422, overrides
        assert r.json()["error"]["field"] == field, (overrides, r.json())


def test_provider_dashboard_lists_only_own_nodes(alice, bob):
    sim_node(alice)
    assert len(alice.get("/api/v1/provider/nodes").json()["nodes"]) == 1
    assert bob.get("/api/v1/provider/nodes").json()["nodes"] == []


def test_other_users_cannot_modify_or_see_nodes(alice, bob):
    node_id = sim_node(alice).json()["node_id"]
    gpu_id = alice.get("/api/v1/provider/nodes").json()["nodes"][0]["gpus"][0]["id"]
    assert bob.patch(f"/api/v1/provider/nodes/{node_id}", json={"enabled": False}).status_code == 404
    assert bob.patch(f"/api/v1/provider/gpus/{gpu_id}", json={"hourly_usd": "0.01"}).status_code == 404
    assert bob.delete(f"/api/v1/provider/nodes/{node_id}").status_code == 404
    assert bob.patch("/api/v1/provider/gpus/1", json={"enabled": False}).status_code == 404  # sample network


def test_price_change_applies_to_new_matches_only(alice, bob):
    sim_node(alice, region="gb-lon", hourly_usd="0.60")
    gpu_id = alice.get("/api/v1/provider/nodes").json()["nodes"][0]["gpus"][0]["id"]
    first = submit(bob, region="gb-lon", est_minutes=60)
    assert first["quote_cents"] == 60
    assert alice.patch(f"/api/v1/provider/gpus/{gpu_id}", json={"hourly_usd": "1.20"}).status_code == 200
    bob.post(f"/api/v1/jobs/{first['id']}/demo/complete")
    done = bob.get(f"/api/v1/jobs/{first['id']}").json()["job"]
    assert done["cost_cents"] == 60  # snapshot of the accepted quote
    second = submit(bob, region="gb-lon", est_minutes=60)
    assert second["quote_cents"] == 120


def test_retire_node_requires_no_active_jobs(alice, bob):
    node_id = sim_node(alice, region="gb-lon").json()["node_id"]
    job = submit(bob, region="gb-lon")
    assert alice.delete(f"/api/v1/provider/nodes/{node_id}").status_code == 409
    bob.post(f"/api/v1/jobs/{job['id']}/demo/complete")
    assert alice.delete(f"/api/v1/provider/nodes/{node_id}").status_code == 200
    assert alice.get("/api/v1/provider/nodes").json()["nodes"] == []
    assert all(g["node"]["id"] != node_id for g in listing(alice)["gpus"])


def test_reliability_score_appears_after_history(app, alice, bob):
    node_id = sim_node(alice, region="gb-lon").json()["node_id"]
    for _ in range(3):
        job = submit(bob, region="gb-lon", est_minutes=1, max_budget_usd="1")
        bob.post(f"/api/v1/jobs/{job['id']}/demo/complete")
    gpu = next(g for g in listing(bob)["gpus"] if g["node"]["id"] == node_id)
    assert gpu["reliability"] == {"completed": 3, "issues": 0, "score": 1.0}


def test_quote_explains_outcomes(anon, alice):
    def quote(**kw):
        body = {"workload": "sim.image", "min_vram_gb": 12, "est_minutes": 60, "max_budget_usd": "2", "region": "any"}
        body.update(kw)
        return anon.post("/api/v1/quotes", json=body).json()

    q = quote(min_vram_gb=24)
    assert q["outcome"] == "match" and q["match"]["model"] == "GeForce RTX 3090" and q["match"]["quote_cents"] == 44
    assert quote(min_vram_gb=80)["outcome"] == "no_capacity"
    assert quote(region="br-sao")["outcome"] == "no_capacity"
    assert quote(workload="agent.hashchain")["outcome"] == "no_capacity"
    over = quote(min_vram_gb=48, max_budget_usd="0.50")
    assert over["outcome"] == "over_budget" and over["cheapest_quote_cents"] == 128
    submit(alice, region="se-sto", min_vram_gb=48)  # occupies the only 48 GB GPU
    assert quote(min_vram_gb=48)["outcome"] == "queue"
    assert anon.post("/api/v1/quotes", json={"workload": "nope", "min_vram_gb": 1, "est_minutes": 1,
                                             "max_budget_usd": "1"}).status_code == 422


def test_simulated_tick_keeps_listing_consistent(app, alice):
    job = submit(alice, region="au-syd")
    assert job["status"] == "assigned"
    tick(app)
    gpu = next(g for g in listing(alice)["gpus"] if g["region"] == "au-syd")
    assert gpu["status"] == "busy"
