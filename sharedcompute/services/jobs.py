"""Job lifecycle: submission, state transitions, settlement and failure handling.

    pending ──assign──▶ assigned ──start──▶ running ──▶ completed
       ▲                   │                   │
       └──── requeue ◀─────┴── interrupted ────┤ (retryable, attempts < max)
                                               └──▶ failed
    pending / assigned / running ──cancel──▶ cancelled

The customer's max budget is held from their balance at submission. Completion charges
the actual cost (never above the quote) and releases the rest; failure and cancellation
release the whole hold. Providers are credited only for completed jobs.
"""
from __future__ import annotations

import json
import sqlite3

from .. import money, regions, workloads
from ..config import Settings
from ..errors import Conflict, Forbidden, InvalidInput, NotFound
from .. import validation as v
from . import ledger

ACTIVE = ("assigned", "running")
FINAL = ("completed", "failed", "cancelled")
STATUSES = ("pending", "assigned", "running", "completed", "failed", "cancelled")
MB_PER_GB = 1024


def event(conn: sqlite3.Connection, job_id: int, kind: str, now: float, detail: str | None = None,
          node_id: int | None = None) -> None:
    conn.execute("INSERT INTO job_events(job_id, node_id, at, kind, detail) VALUES (?, ?, ?, ?, ?)",
                 (job_id, node_id, now, kind, detail))


def submit(conn: sqlite3.Connection, settings: Settings, owner_id: int, body: dict) -> int:
    name = v.text(body.get("name"), "name", "Job name", min_len=2, max_len=80)
    workload = workloads.get(body.get("workload"))  # type: ignore[arg-type]
    if workload is None:
        raise InvalidInput("Choose a supported workload", field="workload")
    vram_gb = v.integer(body.get("min_vram_gb"), "min_vram_gb", "Minimum VRAM", 1, 192)
    minutes = v.integer(body.get("est_minutes"), "est_minutes", "Estimated GPU-minutes", 1, 10_080)
    budget = v.usd(body.get("max_budget_usd"), "max_budget_usd", "Maximum budget")
    region = v.region(body.get("region"), allow_any=True)
    now = settings.now()
    job_id = conn.execute(
        """INSERT INTO jobs(owner_id, name, workload, execution, min_vram_mb, est_minutes,
                            max_budget_cents, region, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (owner_id, name, workload.id, workload.execution, vram_gb * MB_PER_GB, minutes, budget, region, now),
    ).lastrowid
    ledger.hold(conn, owner_id, job_id, budget, now)
    event(conn, job_id, "submitted", now, f"Budget {money.fmt(budget)} held")
    return job_id


def row(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    found = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if found is None:
        raise NotFound("Job not found")
    return found


def owned(conn: sqlite3.Connection, owner_id: int, job_id: int) -> sqlite3.Row:
    job = row(conn, job_id)
    if job["owner_id"] != owner_id:
        raise NotFound("Job not found")  # do not reveal other users' job ids
    return job


def _gpu_summary(conn: sqlite3.Connection, gpu_id: int | None) -> dict | None:
    if gpu_id is None:
        return None
    g = conn.execute(
        """SELECT g.model, g.vram_mb, g.detection, n.name AS node_name, n.region, n.kind
           FROM gpus g JOIN nodes n ON n.id = g.node_id WHERE g.id = ?""", (gpu_id,)
    ).fetchone()
    return {"model": g["model"], "vram_gb": round(g["vram_mb"] / MB_PER_GB, 1), "node_name": g["node_name"],
            "region": g["region"], "region_label": regions.label(g["region"]), "node_kind": g["kind"],
            "detection": g["detection"]}


def serialize(conn: sqlite3.Connection, settings: Settings, job: sqlite3.Row, *, with_events: bool = False) -> dict:
    w = workloads.get(job["workload"])
    data = {
        "id": job["id"], "name": job["name"], "workload": job["workload"],
        "workload_label": w.label if w else job["workload"], "execution": job["execution"],
        "status": job["status"], "min_vram_gb": job["min_vram_mb"] // MB_PER_GB,
        "est_minutes": job["est_minutes"], "max_budget_cents": job["max_budget_cents"],
        "region": job["region"], "region_label": regions.label(job["region"]),
        "attempts": job["attempts"], "max_attempts": settings.max_attempts,
        "hourly_cents": job["hourly_cents"], "quote_cents": job["quote_cents"], "fee_bps": job["fee_bps"],
        "cost_cents": job["cost_cents"], "provider_cents": job["provider_cents"],
        "platform_cents": job["platform_cents"], "billed_seconds": job["billed_seconds"],
        "verified": None if job["verified"] is None else bool(job["verified"]),
        "result": json.loads(job["result"]) if job["result"] else None,
        "failure_reason": job["failure_reason"],
        "gpu": _gpu_summary(conn, job["gpu_id"]),
        "created_at": job["created_at"], "assigned_at": job["assigned_at"],
        "started_at": job["started_at"], "finished_at": job["finished_at"],
        "can_cancel": job["status"] not in FINAL,
        "demo_controls": job["execution"] == "simulated" and job["status"] in ACTIVE,
    }
    if with_events:
        data["events"] = [
            {"at": e["at"], "kind": e["kind"], "detail": e["detail"]}
            for e in conn.execute("SELECT * FROM job_events WHERE job_id = ? ORDER BY id", (job["id"],))
        ]
    return data


def list_owned(conn: sqlite3.Connection, settings: Settings, owner_id: int, status: str | None = None,
               limit: int = 200) -> list[dict]:
    if status is not None and status not in STATUSES:
        raise InvalidInput("Unknown status filter", field="status")
    sql = "SELECT * FROM jobs WHERE owner_id = ?"
    args: list = [owner_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [serialize(conn, settings, j) for j in conn.execute(sql, args).fetchall()]


def status_counts(conn: sqlite3.Connection, owner_id: int | None = None) -> dict:
    sql = "SELECT status, COUNT(*) AS n FROM jobs"
    args: tuple = ()
    if owner_id is not None:
        sql += " WHERE owner_id = ?"
        args = (owner_id,)
    counts = {s: 0 for s in STATUSES}
    for r in conn.execute(sql + " GROUP BY status", args):
        counts[r["status"]] = r["n"]
    return counts


# --- State transitions -------------------------------------------------------------------

def _clear_assignment_sql() -> str:
    return ("gpu_id = NULL, node_id = NULL, provider_id = NULL, hourly_cents = NULL, quote_cents = NULL, "
            "fee_bps = NULL, params = NULL, lease_expires_at = NULL, assigned_at = NULL, started_at = NULL, "
            "cancel_requested = 0")


def fail(conn: sqlite3.Connection, settings: Settings, job: sqlite3.Row, reason: str, *,
         node_id: int | None, verified: bool | None = None, result: dict | None = None) -> None:
    """Final failure: no charge, the full hold is released."""
    now = settings.now()
    conn.execute(
        """UPDATE jobs SET status = 'failed', failure_reason = ?, finished_at = ?, lease_expires_at = NULL,
                  verified = ?, result = ? WHERE id = ?""",
        (reason, now, None if verified is None else int(verified),
         json.dumps(result) if result is not None else None, job["id"]),
    )
    ledger.release(conn, job["owner_id"], job["id"], job["max_budget_cents"], now, "Job failed; hold released")
    event(conn, job["id"], "failed", now, reason, node_id=node_id)


def end_attempt(conn: sqlite3.Connection, settings: Settings, job: sqlite3.Row, reason: str, *,
                retryable: bool) -> str:
    """The current assignment ended without success. Requeue if allowed, otherwise fail."""
    now = settings.now()
    node_id = job["node_id"]
    if retryable and job["attempts"] < settings.max_attempts:
        event(conn, job["id"], "interrupted", now, reason, node_id=node_id)
        conn.execute(f"UPDATE jobs SET status = 'pending', {_clear_assignment_sql()} WHERE id = ?", (job["id"],))
        event(conn, job["id"], "requeued", now, f"Attempt {job['attempts']} of {settings.max_attempts} ended")
        return "pending"
    if retryable:
        reason = f"{reason} (after {job['attempts']} attempts)"
    fail(conn, settings, job, reason, node_id=node_id)
    return "failed"


def complete(conn: sqlite3.Connection, settings: Settings, job: sqlite3.Row, *, billed_seconds: float,
             cost_cents: int, result: dict, verified: bool | None) -> None:
    now = settings.now()
    cost_cents = min(cost_cents, job["quote_cents"])  # never charge above the accepted quote
    provider_cents, platform_cents = money.split_cents(cost_cents, job["fee_bps"])
    conn.execute(
        """UPDATE jobs SET status = 'completed', finished_at = ?, billed_seconds = ?, cost_cents = ?,
                  provider_cents = ?, platform_cents = ?, result = ?, verified = ?, lease_expires_at = NULL
           WHERE id = ?""",
        (now, billed_seconds, cost_cents, provider_cents, platform_cents, json.dumps(result),
         None if verified is None else int(verified), job["id"]),
    )
    ledger.charge(conn, customer_id=job["owner_id"], provider_id=job["provider_id"], job_id=job["id"],
                  held_cents=job["max_budget_cents"], cost_cents=cost_cents, provider_cents=provider_cents,
                  platform_cents=platform_cents, now=now)
    event(conn, job["id"], "completed", now,
          f"Charged {money.fmt(cost_cents)}: provider {money.fmt(provider_cents)}, "
          f"marketplace fee {money.fmt(platform_cents)}", node_id=job["node_id"])


def interrupt_active(conn: sqlite3.Connection, settings: Settings, reason: str, *, node_id: int | None = None,
                     gpu_id: int | None = None) -> int:
    """Requeue (or fail) every active job on a node or GPU that has become unavailable."""
    if node_id is not None:
        rows = conn.execute("SELECT * FROM jobs WHERE node_id = ? AND status IN ('assigned','running')",
                            (node_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs WHERE gpu_id = ? AND status IN ('assigned','running')",
                            (gpu_id,)).fetchall()
    for job in rows:
        end_attempt(conn, settings, job, reason, retryable=True)
    return len(rows)


def cancel(conn: sqlite3.Connection, settings: Settings, owner_id: int, job_id: int) -> None:
    job = owned(conn, owner_id, job_id)
    if job["status"] in FINAL:
        raise Conflict(f"Job is already {job['status']}")
    now = settings.now()
    conn.execute(
        """UPDATE jobs SET status = 'cancelled', finished_at = ?, lease_expires_at = NULL,
                  cancel_requested = ? WHERE id = ?""",
        (now, int(job["status"] == "running"), job_id),
    )
    ledger.release(conn, owner_id, job_id, job["max_budget_cents"], now, "Job cancelled; hold released")
    event(conn, job_id, "cancelled", now, f"Cancelled by customer while {job['status']}")


def simulated_job_for_demo(conn: sqlite3.Connection, user: dict, job_id: int) -> sqlite3.Row:
    """Demo controls are only for simulated jobs, and only for the customer, the node owner or an operator."""
    job = row(conn, job_id)
    if job["owner_id"] != user["id"] and job["provider_id"] != user["id"] and not user["is_operator"]:
        raise NotFound("Job not found")
    if job["execution"] != "simulated":
        raise Forbidden("Demo controls only apply to simulated jobs")
    if job["status"] not in ACTIVE:
        raise Conflict("Only assigned or running simulated jobs can be finished or failed")
    return job


def simulated_result() -> dict:
    return {"simulated": True,
            "note": "No computation was executed. Billed for the estimated GPU-minutes at the quoted rate."}


def finish_simulated(conn: sqlite3.Connection, settings: Settings, job: sqlite3.Row) -> None:
    if job["status"] == "assigned":
        event(conn, job["id"], "started", settings.now(), "Simulated execution started", node_id=job["node_id"])
    complete(conn, settings, job, billed_seconds=job["est_minutes"] * 60, cost_cents=job["quote_cents"],
             result=simulated_result(), verified=None)


def provider_jobs(conn: sqlite3.Connection, provider_id: int, limit: int = 100) -> list[dict]:
    """Jobs on a provider's hardware. Customer identity and job names are not exposed."""
    out = []
    for j in conn.execute(
        """SELECT j.*, g.model, n.name AS node_name FROM jobs j
           JOIN gpus g ON g.id = j.gpu_id JOIN nodes n ON n.id = j.node_id
           WHERE j.provider_id = ? ORDER BY j.id DESC LIMIT ?""", (provider_id, limit)
    ):
        w = workloads.get(j["workload"])
        out.append({"id": j["id"], "workload_label": w.label if w else j["workload"], "execution": j["execution"],
                    "status": j["status"], "gpu_model": j["model"], "node_name": j["node_name"],
                    "billed_seconds": j["billed_seconds"], "cost_cents": j["cost_cents"],
                    "provider_cents": j["provider_cents"], "started_at": j["started_at"],
                    "finished_at": j["finished_at"], "verified": None if j["verified"] is None else bool(j["verified"])})
    return out
