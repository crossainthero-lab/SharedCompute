"""Usage and earnings reporting, derived from completed jobs and the ledger."""
from __future__ import annotations

import sqlite3

from .. import workloads
from ..config import Settings
from . import jobs, ledger

DAY = 86_400


def _transactions(conn: sqlite3.Connection, user_id: int, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT e.id, e.kind, e.job_id, e.memo, e.created_at,
                  SUM(CASE WHEN a.kind = 'available' THEN p.amount_cents ELSE 0 END) AS available_delta,
                  SUM(CASE WHEN a.kind = 'held' THEN p.amount_cents ELSE 0 END) AS held_delta,
                  SUM(CASE WHEN a.kind = 'earnings' THEN p.amount_cents ELSE 0 END) AS earnings_delta
           FROM ledger_entries e
           JOIN ledger_postings p ON p.entry_id = e.id
           JOIN ledger_accounts a ON a.id = p.account_id
           WHERE a.user_id = ?
           GROUP BY e.id ORDER BY e.id DESC LIMIT ?""", (user_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def personal(conn: sqlite3.Connection, settings: Settings, user_id: int) -> dict:
    customer = conn.execute(
        """SELECT COUNT(*) AS jobs, COALESCE(SUM(cost_cents), 0) AS spent_cents,
                  COALESCE(SUM(platform_cents), 0) AS fees_cents, COALESCE(SUM(billed_seconds), 0) AS seconds
           FROM jobs WHERE owner_id = ? AND status = 'completed'""", (user_id,)
    ).fetchone()
    provider = conn.execute(
        """SELECT COUNT(*) AS jobs, COALESCE(SUM(provider_cents), 0) AS earned_cents,
                  COALESCE(SUM(cost_cents), 0) AS gross_cents, COALESCE(SUM(billed_seconds), 0) AS seconds
           FROM jobs WHERE provider_id = ? AND status = 'completed'""", (user_id,)
    ).fetchone()
    return {
        "balances": ledger.user_balances(conn, user_id),
        "customer": {"completed_jobs": customer["jobs"], "spent_cents": customer["spent_cents"],
                     "fees_cents": customer["fees_cents"], "billed_seconds": customer["seconds"],
                     "status_counts": jobs.status_counts(conn, user_id)},
        "provider": {"completed_jobs": provider["jobs"], "earned_cents": provider["earned_cents"],
                     "gross_cents": provider["gross_cents"], "billed_seconds": provider["seconds"]},
        "transactions": _transactions(conn, user_id),
    }


def platform(conn: sqlite3.Connection, settings: Settings) -> dict:
    by_execution = {e: {"completed_jobs": 0, "gross_cents": 0, "provider_cents": 0, "platform_cents": 0,
                        "billed_seconds": 0} for e in ("simulated", "agent")}
    for r in conn.execute(
        """SELECT execution, COUNT(*) AS n, SUM(cost_cents) AS gross, SUM(provider_cents) AS prov,
                  SUM(platform_cents) AS plat, SUM(billed_seconds) AS secs
           FROM jobs WHERE status = 'completed' GROUP BY execution"""
    ):
        by_execution[r["execution"]] = {"completed_jobs": r["n"], "gross_cents": r["gross"],
                                        "provider_cents": r["prov"], "platform_cents": r["plat"],
                                        "billed_seconds": r["secs"]}
    totals = {k: sum(e[k] for e in by_execution.values()) for k in by_execution["agent"]}

    sums = {r["kind"]: r["total"] for r in conn.execute(
        """SELECT a.kind, COALESCE(SUM(p.amount_cents), 0) AS total
           FROM ledger_accounts a LEFT JOIN ledger_postings p ON p.account_id = a.id GROUP BY a.kind""")}
    imbalance = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM ledger_postings").fetchone()[0]

    now = settings.now()
    today = int(now // DAY) * DAY
    daily = {today - i * DAY: 0 for i in range(13, -1, -1)}
    for r in conn.execute(
        """SELECT CAST(finished_at / 86400 AS INTEGER) * 86400 AS day, SUM(cost_cents) AS gross
           FROM jobs WHERE status = 'completed' AND finished_at >= ? GROUP BY day""", (today - 13 * DAY,)
    ):
        if r["day"] in daily:
            daily[r["day"]] = r["gross"]

    recent = []
    for j in conn.execute(
        """SELECT j.id, j.workload, j.execution, j.cost_cents, j.provider_cents, j.platform_cents,
                  j.billed_seconds, j.finished_at, j.verified, g.model
           FROM jobs j LEFT JOIN gpus g ON g.id = j.gpu_id
           WHERE j.status = 'completed' ORDER BY j.finished_at DESC LIMIT 50"""
    ):
        w = workloads.get(j["workload"])
        recent.append({**dict(j), "workload_label": w.label if w else j["workload"],
                       "verified": None if j["verified"] is None else bool(j["verified"])})

    return {
        "fee_bps": settings.platform_fee_bps,
        "totals": totals,
        "by_execution": by_execution,
        "ledger": {
            "platform_revenue_cents": sums.get("revenue", 0),
            "provider_earnings_cents": sums.get("earnings", 0),
            "customer_available_cents": sums.get("available", 0),
            "customer_held_cents": sums.get("held", 0),
            "demo_credits_issued_cents": -sums.get("demo_funding", 0),
            "balanced": imbalance == 0,
        },
        "status_counts": jobs.status_counts(conn),
        "participants": {
            "customers": conn.execute("SELECT COUNT(DISTINCT owner_id) FROM jobs").fetchone()[0],
            "providers": conn.execute(
                """SELECT COUNT(DISTINCT n.owner_id) FROM nodes n JOIN users u ON u.id = n.owner_id
                   WHERE n.retired = 0 AND u.is_system = 0""").fetchone()[0],
        },
        "daily_gross": [{"day": d, "gross_cents": c} for d, c in daily.items()],
        "recent_transactions": recent,
        "generated_at": now,
    }
