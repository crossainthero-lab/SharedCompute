"""Matching scheduler and the periodic tick.

A GPU is eligible for a job when it is online, enabled and idle, supports the workload,
has enough VRAM, satisfies the region constraint, and its quote for the job's estimated
minutes fits within the job's budget. Among eligible GPUs the cheapest wins; ties go to the
node with the better completion history, then to the older listing. Jobs are matched in
submission order.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

from .. import money, regions, workloads
from .. import validation as v
from ..config import Settings
from ..errors import InvalidInput
from . import jobs, nodes


def _smoothed(reliability: dict, node_id: int) -> float:
    r = reliability.get(node_id)
    if not r:
        return 0.5
    return (r["completed"] + 1) / (r["completed"] + r["issues"] + 2)


def _matches(job: dict, r: sqlite3.Row, workload: workloads.Workload, settings: Settings) -> bool:
    return (nodes.supports(workload, r)
            and r["vram_mb"] >= job["min_vram_mb"]
            and (job["region"] == regions.ANY or r["region"] == job["region"])
            and (settings.allow_self_match or r["owner_id"] != job["owner_id"]))


def _ranked(rows: list[sqlite3.Row], reliability: dict) -> list[sqlite3.Row]:
    return sorted(rows, key=lambda r: (r["hourly_cents"], -_smoothed(reliability, r["node_id"]), r["id"]))


def _assign(conn: sqlite3.Connection, settings: Settings, job: sqlite3.Row, gpu: sqlite3.Row, quote: int) -> None:
    now = settings.now()
    workload = workloads.get(job["workload"])
    params = workloads.issue_params(workload, hashchain_iterations=settings.hashchain_iterations)
    lease = now + settings.claim_timeout_seconds if gpu["kind"] == "agent" else None
    conn.execute(
        """UPDATE jobs SET status = 'assigned', gpu_id = ?, node_id = ?, provider_id = ?, hourly_cents = ?,
                  quote_cents = ?, fee_bps = ?, params = ?, attempts = attempts + 1, assigned_at = ?,
                  lease_expires_at = ?, cancel_requested = 0
           WHERE id = ? AND status = 'pending'""",
        (gpu["id"], gpu["node_id"], gpu["owner_id"], gpu["hourly_cents"], quote, settings.platform_fee_bps,
         json.dumps(params), now, lease, job["id"]),
    )
    jobs.event(conn, job["id"], "assigned", now,
               f"{gpu['model']} on {gpu['node_name']} at {money.fmt(gpu['hourly_cents'])}/GPU-hour, "
               f"quote {money.fmt(quote)}", node_id=gpu["node_id"])


def schedule(conn: sqlite3.Connection, settings: Settings) -> int:
    pending = conn.execute("SELECT * FROM jobs WHERE status = 'pending' ORDER BY id").fetchall()
    if not pending:
        return 0
    now = settings.now()
    reliability = nodes.node_reliability(conn)
    free = _ranked([r for r in nodes.listing_rows(conn) if nodes.gpu_state(r, settings, now) == "available"],
                   reliability)
    taken: set[int] = set()
    node_load: Counter = Counter()
    assigned = 0
    for job in pending:
        workload = workloads.get(job["workload"])
        for gpu in free:
            if gpu["id"] in taken:
                continue
            if gpu["kind"] == "agent" and gpu["node_active"] + node_load[gpu["node_id"]] >= gpu["max_concurrency"]:
                continue
            if not _matches(dict(job), gpu, workload, settings):
                continue
            quote = money.quote_cents(gpu["hourly_cents"], job["est_minutes"])
            if quote > job["max_budget_cents"]:
                break  # GPUs are ordered by price, so nothing later is affordable either
            _assign(conn, settings, job, gpu, quote)
            taken.add(gpu["id"])
            node_load[gpu["node_id"]] += 1
            assigned += 1
            break
    return assigned


def preview(conn: sqlite3.Connection, settings: Settings, owner_id: int, body: dict) -> dict:
    """Dry run of the scheduler for a prospective job, explaining why it would match or wait."""
    workload = workloads.get(body.get("workload"))  # type: ignore[arg-type]
    if workload is None:
        raise InvalidInput("Choose a supported workload", field="workload")
    job = {
        "workload": workload.id,
        "min_vram_mb": v.integer(body.get("min_vram_gb"), "min_vram_gb", "Minimum VRAM", 1, 192) * nodes.MB_PER_GB,
        "est_minutes": v.integer(body.get("est_minutes"), "est_minutes", "Estimated GPU-minutes", 1, 10_080),
        "max_budget_cents": v.usd(body.get("max_budget_usd"), "max_budget_usd", "Maximum budget"),
        "region": v.region(body.get("region"), allow_any=True),
        "owner_id": owner_id,
    }
    now = settings.now()
    reliability = nodes.node_reliability(conn)
    rows = nodes.listing_rows(conn)

    def result(outcome: str, reason: str, **extra) -> dict:
        return {"outcome": outcome, "reason": reason, "match": None, "cheapest_quote_cents": None,
                "compatible": 0, "available_now": 0, **extra}

    stage = [r for r in rows if nodes.supports(workload, r)]
    if not stage:
        return result("no_capacity", f"No listed GPU can run “{workload.label}” yet. "
                      "The job would wait until one is connected.")
    stage = [r for r in stage if r["vram_mb"] >= job["min_vram_mb"]]
    if not stage:
        return result("no_capacity", f"No GPU that runs this workload has {job['min_vram_mb'] // 1024} GB "
                      "or more VRAM.")
    stage = [r for r in stage if job["region"] == regions.ANY or r["region"] == job["region"]]
    if not stage:
        return result("no_capacity", f"No compatible GPU is listed in {regions.label(job['region'])}.")
    if not settings.allow_self_match:
        stage = [r for r in stage if r["owner_id"] != owner_id]
        if not stage:
            return result("no_capacity", "The only compatible GPUs are your own.")
    compatible = _ranked(stage, reliability)
    quotes = {r["id"]: money.quote_cents(r["hourly_cents"], job["est_minutes"]) for r in compatible}
    cheapest = min(quotes.values())
    affordable = [r for r in compatible if quotes[r["id"]] <= job["max_budget_cents"]]
    if not affordable:
        return result("over_budget",
                      f"The cheapest compatible GPU would cost {money.fmt(cheapest)} for "
                      f"{job['est_minutes']} minutes, above your {money.fmt(job['max_budget_cents'])} budget.",
                      cheapest_quote_cents=cheapest, compatible=len(compatible))
    available = [r for r in affordable if nodes.gpu_state(r, settings, now) == "available"]
    if not available:
        return result("queue", f"{len(affordable)} compatible GPU(s) fit your budget but none is free right "
                      "now. The job will wait in the queue.",
                      cheapest_quote_cents=cheapest, compatible=len(compatible))
    best = available[0]
    return {
        "outcome": "match",
        "reason": "Would be assigned immediately if still free at submission.",
        "match": {"gpu_id": best["id"], "model": best["model"], "vram_gb": round(best["vram_mb"] / 1024, 1),
                  "node_name": best["node_name"], "region_label": regions.label(best["region"]),
                  "execution": best["kind"], "hourly_cents": best["hourly_cents"],
                  "quote_cents": quotes[best["id"]]},
        "cheapest_quote_cents": cheapest, "compatible": len(compatible), "available_now": len(available),
    }


def tick(conn: sqlite3.Connection, settings: Settings) -> None:
    """Advance time-based state: liveness, leases, time limits, the simulator, then matching."""
    now = settings.now()

    for job in conn.execute(
        """SELECT j.* FROM jobs j JOIN nodes n ON n.id = j.node_id
           WHERE j.status IN ('assigned','running') AND n.kind = 'agent'
             AND (n.last_seen_at IS NULL OR n.last_seen_at < ?)""", (now - settings.node_ttl_seconds,)
    ).fetchall():
        jobs.end_attempt(conn, settings, job, "Provider node stopped sending heartbeats", retryable=True)

    for job in conn.execute(
        "SELECT * FROM jobs WHERE status = 'assigned' AND execution = 'agent' AND lease_expires_at < ?", (now,)
    ).fetchall():
        jobs.end_attempt(conn, settings, job, "Provider agent did not claim the job in time", retryable=True)

    for job in conn.execute(
        """SELECT * FROM jobs WHERE status = 'running' AND execution = 'agent'
             AND started_at + est_minutes * 60 + ? < ?""", (settings.time_limit_grace_seconds, now)
    ).fetchall():
        jobs.fail(conn, settings, job, "Exceeded the job's time limit", node_id=job["node_id"])

    for job in conn.execute(
        "SELECT * FROM jobs WHERE status = 'assigned' AND execution = 'simulated'"
    ).fetchall():
        conn.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (now, job["id"]))
        jobs.event(conn, job["id"], "started", now, "Simulated execution started", node_id=job["node_id"])

    for job in conn.execute(
        "SELECT * FROM jobs WHERE status = 'running' AND execution = 'simulated' AND started_at <= ?",
        (now - settings.simulation_seconds,),
    ).fetchall():
        jobs.finish_simulated(conn, settings, job)

    schedule(conn, settings)
