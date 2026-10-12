from conftest import balances, get_job, ledger_invariants, make_user, submit, tick


def test_hold_is_taken_at_submission_and_settled_on_completion(app, alice):
    job = submit(alice, min_vram_gb=24, max_budget_usd="2.00")
    assert balances(alice) == {"available_cents": 9_800, "held_cents": 200, "earnings_cents": 0}
    alice.post(f"/api/v1/jobs/{job['id']}/demo/complete")
    assert balances(alice) == {"available_cents": 10_000 - 44, "held_cents": 0, "earnings_cents": 0}
    ledger_invariants(app)


def test_insufficient_balance_is_rejected_atomically(app):
    poor = make_user(app, "poor@example.com", credits="1.00")
    r = poor.post("/api/v1/jobs", json={"name": "Big", "workload": "sim.image", "min_vram_gb": 12,
                                        "est_minutes": 60, "max_budget_usd": "5.00", "region": "any"})
    assert r.status_code == 402 and r.json()["error"]["code"] == "insufficient_funds"
    assert poor.get("/api/v1/jobs").json()["jobs"] == []  # no orphaned job row
    assert balances(poor)["available_cents"] == 100
    ledger_invariants(app)


def test_budget_caps_the_match(alice):
    # 60 minutes on the cheapest 24 GB GPU costs $0.44; a $0.40 budget cannot afford any 24 GB GPU.
    job = submit(alice, min_vram_gb=24, max_budget_usd="0.40")
    assert job["status"] == "pending" and job["gpu"] is None
    # A longer job only fits a cheaper GPU: 30 min at $0.44 = $0.22 fits; at $0.67 = $0.34 would too,
    # but the scheduler takes the cheapest.
    job2 = submit(alice, min_vram_gb=24, est_minutes=30, max_budget_usd="0.25")
    assert job2["gpu"]["model"] == "GeForce RTX 3090" and job2["quote_cents"] == 22


def test_skips_unaffordable_and_picks_next_compatible(alice):
    # VRAM 48 only exists at $1.28/h; 10 minutes = $0.21.
    assert submit(alice, min_vram_gb=48, est_minutes=10, max_budget_usd="0.20")["status"] == "pending"
    assert submit(alice, min_vram_gb=48, est_minutes=10, max_budget_usd="0.21")["status"] == "assigned"


def test_region_and_vram_constraints(alice):
    job = submit(alice, region="se-sto", min_vram_gb=12)
    assert job["gpu"]["region"] == "se-sto"
    assert submit(alice, region="au-syd", min_vram_gb=24)["status"] == "pending"


def test_queue_is_fifo(app, alice, bob):
    first = submit(alice, region="au-syd")
    second = submit(bob, region="au-syd")
    third = submit(alice, region="au-syd")
    assert [first["status"], second["status"], third["status"]] == ["assigned", "pending", "pending"]
    alice.post(f"/api/v1/jobs/{first['id']}/demo/complete")
    assert get_job(bob, second["id"])["status"] == "assigned"
    assert get_job(alice, third["id"])["status"] == "pending"


def test_simulator_completes_after_configured_time(app, alice, clock):
    job = submit(alice)
    tick(app)
    assert get_job(alice, job["id"])["status"] == "running"
    clock.advance(app.state.settings.simulation_seconds - 1)
    tick(app)
    assert get_job(alice, job["id"])["status"] == "running"
    clock.advance(2)
    tick(app)
    done = get_job(alice, job["id"])
    assert done["status"] == "completed"
    assert done["billed_seconds"] == 60 * 60  # simulated jobs bill their estimate
    assert done["result"]["simulated"] is True
    assert [e["kind"] for e in done["events"]] == ["submitted", "assigned", "started", "completed"]
    ledger_invariants(app)


def test_provider_earnings_and_platform_revenue(app, alice, bob):
    bob.post("/api/v1/provider/nodes", json={"kind": "simulated", "name": "Bob's rig", "region": "gb-lon",
                                            "gpu_model": "RTX 4080", "vram_gb": 16, "hourly_usd": "0.50"})
    for _ in range(3):
        job = submit(alice, region="gb-lon", est_minutes=60, max_budget_usd="1")
        alice.post(f"/api/v1/jobs/{job['id']}/demo/complete")
    assert balances(bob)["earnings_cents"] == 3 * 45
    econ = bob.get("/api/v1/economics").json()["personal"]
    assert econ["provider"]["earned_cents"] == 135 and econ["provider"]["completed_jobs"] == 3
    served = bob.get("/api/v1/provider/jobs").json()["jobs"]
    assert len(served) == 3 and all(j["provider_cents"] == 45 for j in served)
    assert "name" not in served[0]  # customers' job names are not shared with providers
    node = bob.get("/api/v1/provider/nodes").json()["nodes"][0]
    assert node["earned_cents"] == 135 and node["completed_jobs"] == 3
    with app.state.db.read() as conn:
        revenue = conn.execute("""SELECT SUM(p.amount_cents) FROM ledger_postings p JOIN ledger_accounts a
                                  ON a.id = p.account_id WHERE a.code = 'platform:revenue'""").fetchone()[0]
    assert revenue == 15
    ledger_invariants(app)


def test_failure_releases_hold_and_pays_nobody(app, alice):
    job = submit(alice, min_vram_gb=24)
    r = alice.post(f"/api/v1/jobs/{job['id']}/demo/fail")
    assert r.status_code == 200
    failed = get_job(alice, job["id"])
    assert failed["status"] == "failed" and failed["cost_cents"] is None
    assert "Simulated provider failure" in failed["failure_reason"]
    assert balances(alice) == {"available_cents": 10_000, "held_cents": 0, "earnings_cents": 0}
    ledger_invariants(app)


def test_cancel_in_each_state(app, alice):
    pending = submit(alice, min_vram_gb=80)
    assigned = submit(alice, region="au-syd")
    running = submit(alice, region="se-sto")
    tick(app)
    assigned_now = submit(alice, region="sg-sin")
    for job in (pending, assigned_now, running):
        r = alice.post(f"/api/v1/jobs/{job['id']}/cancel")
        assert r.status_code == 200 and r.json()["job"]["status"] == "cancelled"
    done = assigned
    alice.post(f"/api/v1/jobs/{done['id']}/demo/complete")
    assert alice.post(f"/api/v1/jobs/{done['id']}/cancel").status_code == 409
    b = balances(alice)
    assert b["held_cents"] == 0 and b["available_cents"] == 10_000 - get_job(alice, done["id"])["cost_cents"]
    ledger_invariants(app)


def test_requeue_on_pause_then_fail_after_max_attempts(app, alice, bob):
    bob.post("/api/v1/provider/nodes", json={"kind": "simulated", "name": "Flaky", "region": "br-sao",
                                            "gpu_model": "RTX 3070", "vram_gb": 8, "hourly_usd": "0.20"})
    node_id = bob.get("/api/v1/provider/nodes").json()["nodes"][0]["id"]
    job = submit(alice, region="br-sao", min_vram_gb=8)
    for attempt in range(1, 4):
        assert get_job(alice, job["id"])["attempts"] == attempt
        bob.patch(f"/api/v1/provider/nodes/{node_id}", json={"enabled": False})
        state = get_job(alice, job["id"])["status"]
        if attempt < 3:
            assert state == "pending"
            bob.patch(f"/api/v1/provider/nodes/{node_id}", json={"enabled": True})
    final = get_job(alice, job["id"])
    assert final["status"] == "failed" and "after 3 attempts" in final["failure_reason"]
    kinds = [e["kind"] for e in final["events"]]
    assert kinds.count("interrupted") == 2 and kinds[-1] == "failed"
    track = next(g for g in bob.get("/api/v1/marketplace").json()["gpus"] if g["node"]["id"] == node_id)["reliability"]
    assert track == {"completed": 0, "issues": 3, "score": 0.0}
    assert balances(alice)["held_cents"] == 0
    ledger_invariants(app)


def test_jobs_are_private_to_their_owner(alice, bob):
    job = submit(alice)
    assert bob.get(f"/api/v1/jobs/{job['id']}").status_code == 404
    assert bob.post(f"/api/v1/jobs/{job['id']}/cancel").status_code == 404
    assert bob.post(f"/api/v1/jobs/{job['id']}/demo/complete").status_code == 404
    assert bob.get("/api/v1/jobs").json()["jobs"] == []


def test_demo_controls_only_for_simulated_jobs(app, alice):
    job = submit(alice, workload="agent.hashchain", min_vram_gb=1)  # no agents: stays pending
    assert job["status"] == "pending"
    assert alice.post(f"/api/v1/jobs/{job['id']}/demo/complete").status_code == 403


def test_status_filter_and_counts(alice):
    submit(alice)
    submit(alice, min_vram_gb=80)
    data = alice.get("/api/v1/jobs?status=pending").json()
    assert len(data["jobs"]) == 1 and data["counts"]["pending"] == 1 and data["counts"]["assigned"] == 1
    assert alice.get("/api/v1/jobs?status=bogus").status_code == 422


def test_self_match_policy(app, alice):
    alice.post("/api/v1/provider/nodes", json={"kind": "simulated", "name": "Mine", "region": "ca-tor",
                                              "gpu_model": "RTX 3060", "vram_gb": 12, "hourly_usd": "0.10"})
    assert submit(alice, region="ca-tor")["status"] == "assigned"
    app.state.settings.allow_self_match = False
    assert submit(alice, region="ca-tor")["status"] == "pending"


def test_operator_platform_economics(app, alice, bob):
    from sharedcompute.services import accounts

    job = submit(alice, min_vram_gb=24)
    alice.post(f"/api/v1/jobs/{job['id']}/demo/complete")
    with app.state.db.write() as conn:
        accounts.set_operator(conn, "bob@example.com")
    platform = bob.get("/api/v1/economics").json()["platform"]
    assert platform["totals"]["gross_cents"] == 44
    assert platform["ledger"]["platform_revenue_cents"] == 4
    assert platform["ledger"]["balanced"] is True
    assert platform["ledger"]["demo_credits_issued_cents"] == 20_000
    assert platform["by_execution"]["simulated"]["completed_jobs"] == 1
    assert platform["by_execution"]["agent"]["completed_jobs"] == 0
    assert platform["daily_gross"][-1]["gross_cents"] == 44
    assert platform["recent_transactions"][0]["id"] == job["id"]


def test_demo_topup_limits(app, alice):
    assert alice.post("/api/v1/wallet/demo-topup", json={"amount_usd": "1000.01"}).status_code == 422
    assert alice.post("/api/v1/wallet/demo-topup", json={"amount_usd": "0"}).status_code == 422
    for _ in range(9):
        assert alice.post("/api/v1/wallet/demo-topup", json={"amount_usd": "1000"}).status_code == 200
    assert alice.post("/api/v1/wallet/demo-topup", json={"amount_usd": "1000"}).status_code == 422  # $10k cap
    app.state.settings.demo_credits_enabled = False
    assert alice.post("/api/v1/wallet/demo-topup", json={"amount_usd": "1"}).status_code == 403
