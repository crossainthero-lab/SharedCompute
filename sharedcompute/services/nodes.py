"""Provider nodes and their GPUs: registration, pricing, availability and marketplace listings."""
from __future__ import annotations

import json
import sqlite3

from .. import regions, workloads
from .. import validation as v
from ..config import Settings
from ..errors import Conflict, InvalidInput, NotFound
from ..security import NODE_PREFIX, new_token, token_hash
from . import accounts, jobs

MB_PER_GB = 1024
MAX_HOURLY_CENTS = 100_00  # $100/GPU-hour ceiling guards against typos

SAMPLE_NETWORK = [
    # name, region, gpu model, vram GB, $/GPU-hour (carried over from the v0.1 draft)
    ("Sample node · Sydney", "au-syd", "GeForce RTX 3060", 12, "0.24"),
    ("Sample node · Singapore", "sg-sin", "GeForce RTX 3090", 24, "0.44"),
    ("Sample node · Frankfurt", "de-fra", "GeForce RTX 4090", 24, "0.67"),
    ("Sample node · Stockholm", "se-sto", "RTX 6000 Ada Generation", 48, "1.28"),
]

LISTING_SQL = """
SELECT g.*, n.name AS node_name, n.region, n.kind, n.enabled AS node_enabled, n.last_seen_at,
       n.owner_id, n.capabilities, n.max_concurrency, u.display_name AS provider_name,
       (SELECT COUNT(*) FROM jobs j WHERE j.gpu_id = g.id AND j.status IN ('assigned','running')) AS gpu_active,
       (SELECT COUNT(*) FROM jobs j WHERE j.node_id = n.id AND j.status IN ('assigned','running')) AS node_active
FROM gpus g JOIN nodes n ON n.id = g.node_id JOIN users u ON u.id = n.owner_id
WHERE n.retired = 0 AND g.present = 1
"""


def node_online(node: sqlite3.Row, settings: Settings, now: float) -> bool:
    if node["kind"] == "simulated":
        return True
    seen = node["last_seen_at"]
    return seen is not None and now - seen <= settings.node_ttl_seconds


def gpu_state(r: sqlite3.Row, settings: Settings, now: float) -> str:
    """available | busy | paused | offline. Shared by listings and the scheduler."""
    if not node_online(r, settings, now):
        return "offline"
    if not r["node_enabled"] or not r["enabled"]:
        return "paused"
    if r["gpu_active"] or (r["kind"] == "agent" and r["node_active"] >= r["max_concurrency"]):
        return "busy"
    return "available"


def supports(workload: workloads.Workload, r: sqlite3.Row) -> bool:
    if workload.execution == "simulated":
        return r["kind"] == "simulated"
    return (r["kind"] == "agent" and workload.id in json.loads(r["capabilities"])
            and (not workload.requires_cuda or bool(r["cuda"])))


def node_reliability(conn: sqlite3.Connection) -> dict[int, dict]:
    """Per-node history from job events. A score is only shown once there is enough history."""
    stats: dict[int, dict] = {}
    for r in conn.execute(
        """SELECT node_id, SUM(kind = 'completed') AS completed,
                  SUM(kind IN ('interrupted', 'failed')) AS issues
           FROM job_events WHERE node_id IS NOT NULL GROUP BY node_id"""
    ):
        total = r["completed"] + r["issues"]
        stats[r["node_id"]] = {"completed": r["completed"], "issues": r["issues"],
                               "score": round(r["completed"] / total, 3) if total >= 3 else None}
    return stats


def listing_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(LISTING_SQL + " ORDER BY g.hourly_cents, g.id").fetchall()


def _listing(r: sqlite3.Row, settings: Settings, now: float, reliability: dict) -> dict:
    return {
        "id": r["id"], "model": r["model"], "vendor": r["vendor"],
        "vram_gb": round(r["vram_mb"] / MB_PER_GB, 1), "hourly_cents": r["hourly_cents"],
        "region": r["region"], "region_label": regions.label(r["region"]),
        "status": gpu_state(r, settings, now), "execution": r["kind"], "detection": r["detection"],
        "cuda": bool(r["cuda"]), "integrated": bool(r["integrated"]),
        "node": {"id": r["node_id"], "name": r["node_name"]}, "provider_name": r["provider_name"],
        "reliability": reliability.get(r["node_id"], {"completed": 0, "issues": 0, "score": None}),
        "workloads": [w.id for w in workloads.CATALOGUE.values() if supports(w, r)],
        "metrics": _metrics(r, settings, now),
    }


def _metrics(r: sqlite3.Row, settings: Settings, now: float) -> dict | None:
    if r["metrics_at"] is None or now - r["metrics_at"] > settings.node_ttl_seconds:
        return None
    return {"util_percent": r["util_percent"], "mem_used_mb": r["mem_used_mb"], "temperature_c": r["temperature_c"]}


def marketplace(conn: sqlite3.Connection, settings: Settings) -> dict:
    now = settings.now()
    reliability = node_reliability(conn)
    gpus = [_listing(r, settings, now, reliability) for r in listing_rows(conn)]
    available = [g for g in gpus if g["status"] == "available"]
    return {
        "gpus": gpus,
        "summary": {
            "listed": len(gpus),
            "available": len(available),
            "busy": sum(g["status"] == "busy" for g in gpus),
            "agent_connected": sum(g["execution"] == "agent" and g["status"] != "offline" for g in gpus),
            "simulated": sum(g["execution"] == "simulated" for g in gpus),
            "regions": len({g["region"] for g in gpus}),
            "lowest_available_cents": min((g["hourly_cents"] for g in available), default=None),
        },
    }


# --- Provider management ------------------------------------------------------------------

def _owned_node(conn: sqlite3.Connection, owner_id: int, node_id: int) -> sqlite3.Row:
    node = conn.execute("SELECT * FROM nodes WHERE id = ? AND retired = 0", (node_id,)).fetchone()
    if node is None or node["owner_id"] != owner_id:
        raise NotFound("Node not found")
    return node


def _owned_gpu(conn: sqlite3.Connection, owner_id: int, gpu_id: int) -> sqlite3.Row:
    gpu = conn.execute(
        "SELECT g.*, n.owner_id FROM gpus g JOIN nodes n ON n.id = g.node_id WHERE g.id = ? AND n.retired = 0",
        (gpu_id,),
    ).fetchone()
    if gpu is None or gpu["owner_id"] != owner_id:
        raise NotFound("GPU not found")
    return gpu


def infer_vendor(model: str) -> str:
    m = model.lower()
    if any(k in m for k in ("nvidia", "geforce", "rtx", "gtx", "tesla", "quadro", "a100", "h100", "l40")):
        return "NVIDIA"
    if any(k in m for k in ("amd", "radeon", "instinct")):
        return "AMD"
    if any(k in m for k in ("intel", "arc ")):
        return "Intel"
    if "apple" in m:
        return "Apple"
    return "Unknown"


def _price(value: object) -> int:
    return v.usd(value, "hourly_usd", "Price per GPU-hour", max_cents=MAX_HOURLY_CENTS)


def create_node(conn: sqlite3.Connection, settings: Settings, owner_id: int, body: dict) -> dict:
    """Register a node. Returns the node id and, for agent nodes, the one-time agent token."""
    kind = body.get("kind")
    if kind not in ("simulated", "agent"):
        raise InvalidInput("Choose a simulated listing or an agent-connected machine", field="kind")
    name = v.text(body.get("name"), "name", "Node name", min_len=2, max_len=60)
    region = v.region(body.get("region"))
    price = _price(body.get("hourly_usd"))
    now = settings.now()
    token = new_token(NODE_PREFIX) if kind == "agent" else None
    node_id = conn.execute(
        """INSERT INTO nodes(owner_id, name, region, kind, token_hash, default_hourly_cents, capabilities, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (owner_id, name, region, kind, token_hash(token) if token else None, price,
         json.dumps([w for w, wl in workloads.CATALOGUE.items() if wl.execution == "simulated"])
         if kind == "simulated" else "[]", now),
    ).lastrowid
    if kind == "simulated":
        model = v.text(body.get("gpu_model"), "gpu_model", "GPU model", min_len=2, max_len=60)
        vram_gb = v.integer(body.get("vram_gb"), "vram_gb", "VRAM", 1, 192)
        conn.execute(
            """INSERT INTO gpus(node_id, slot, vendor, model, vram_mb, detection, hourly_cents, created_at)
               VALUES (?, 0, ?, ?, ?, 'self_reported', ?, ?)""",
            (node_id, infer_vendor(model), model, vram_gb * MB_PER_GB, price, now),
        )
    return {"id": node_id, "agent_token": token}


def update_node(conn: sqlite3.Connection, settings: Settings, owner_id: int, node_id: int, body: dict) -> None:
    node = _owned_node(conn, owner_id, node_id)
    if "name" in body:
        conn.execute("UPDATE nodes SET name = ? WHERE id = ?",
                     (v.text(body["name"], "name", "Node name", min_len=2, max_len=60), node_id))
    if "region" in body:
        conn.execute("UPDATE nodes SET region = ? WHERE id = ?", (v.region(body["region"]), node_id))
    if "enabled" in body:
        enabled = v.boolean(body["enabled"], "enabled", "Enabled")
        conn.execute("UPDATE nodes SET enabled = ? WHERE id = ?", (int(enabled), node_id))
        if node["enabled"] and not enabled:
            jobs.interrupt_active(conn, settings, "Provider paused the node", node_id=node_id)


def update_gpu(conn: sqlite3.Connection, settings: Settings, owner_id: int, gpu_id: int, body: dict) -> None:
    gpu = _owned_gpu(conn, owner_id, gpu_id)
    if "hourly_usd" in body:
        # Active jobs keep their quoted rate; the new price applies to future matches.
        conn.execute("UPDATE gpus SET hourly_cents = ? WHERE id = ?", (_price(body["hourly_usd"]), gpu_id))
    if "enabled" in body:
        enabled = v.boolean(body["enabled"], "enabled", "Enabled")
        conn.execute("UPDATE gpus SET enabled = ? WHERE id = ?", (int(enabled), gpu_id))
        if gpu["enabled"] and not enabled:
            jobs.interrupt_active(conn, settings, "Provider paused the GPU", gpu_id=gpu_id)


def rotate_token(conn: sqlite3.Connection, owner_id: int, node_id: int) -> str:
    node = _owned_node(conn, owner_id, node_id)
    if node["kind"] != "agent":
        raise Conflict("Only agent-connected nodes have tokens")
    token = new_token(NODE_PREFIX)
    conn.execute("UPDATE nodes SET token_hash = ? WHERE id = ?", (token_hash(token), node_id))
    return token


def retire_node(conn: sqlite3.Connection, settings: Settings, owner_id: int, node_id: int) -> None:
    _owned_node(conn, owner_id, node_id)
    active = conn.execute("SELECT COUNT(*) FROM jobs WHERE node_id = ? AND status IN ('assigned','running')",
                          (node_id,)).fetchone()[0]
    if active:
        raise Conflict("Pause the node and wait for active jobs to finish before removing it")
    # Soft delete: job history and ledger entries keep referring to the node.
    conn.execute("UPDATE nodes SET retired = 1, enabled = 0, token_hash = NULL WHERE id = ?", (node_id,))


def owned_nodes(conn: sqlite3.Connection, settings: Settings, owner_id: int) -> list[dict]:
    now = settings.now()
    reliability = node_reliability(conn)
    earnings = {r["node_id"]: dict(r) for r in conn.execute(
        """SELECT node_id, COUNT(*) AS completed, COALESCE(SUM(provider_cents), 0) AS earned_cents,
                  COALESCE(SUM(billed_seconds), 0) AS billed_seconds
           FROM jobs WHERE provider_id = ? AND status = 'completed' GROUP BY node_id""", (owner_id,))}
    gpu_rows: dict[int, list] = {}
    for r in conn.execute(LISTING_SQL + " AND n.owner_id = ? ORDER BY g.slot", (owner_id,)):
        gpu_rows.setdefault(r["node_id"], []).append({
            "id": r["id"], "slot": r["slot"], "model": r["model"], "vendor": r["vendor"],
            "vram_gb": round(r["vram_mb"] / MB_PER_GB, 1), "hourly_cents": r["hourly_cents"],
            "enabled": bool(r["enabled"]), "status": gpu_state(r, settings, now), "detection": r["detection"],
            "cuda": bool(r["cuda"]), "integrated": bool(r["integrated"]),
            "compute_capability": r["compute_capability"], "driver_version": r["driver_version"],
            "metrics": _metrics(r, settings, now),
        })
    out = []
    for n in conn.execute("SELECT * FROM nodes WHERE owner_id = ? AND retired = 0 ORDER BY id", (owner_id,)):
        e = earnings.get(n["id"], {"completed": 0, "earned_cents": 0, "billed_seconds": 0})
        out.append({
            "id": n["id"], "name": n["name"], "region": n["region"], "region_label": regions.label(n["region"]),
            "kind": n["kind"], "enabled": bool(n["enabled"]), "online": node_online(n, settings, now),
            "last_seen_at": n["last_seen_at"], "agent_version": n["agent_version"], "platform": n["platform"],
            "capabilities": json.loads(n["capabilities"]),
            "default_hourly_cents": n["default_hourly_cents"], "gpus": gpu_rows.get(n["id"], []),
            "completed_jobs": e["completed"], "earned_cents": e["earned_cents"],
            "billed_seconds": e["billed_seconds"],
            "reliability": reliability.get(n["id"], {"completed": 0, "issues": 0, "score": None}),
        })
    return out


def seed_sample_network(conn: sqlite3.Connection, settings: Settings) -> None:
    """Simulated listings owned by a system account, so the marketplace is testable out of the box."""
    owner = accounts.ensure_system_user(conn, settings, accounts.SAMPLE_NETWORK_EMAIL, "Sample network")
    if conn.execute("SELECT 1 FROM nodes WHERE owner_id = ?", (owner,)).fetchone():
        return
    for name, region, model, vram, price in SAMPLE_NETWORK:
        create_node(conn, settings, owner, {"kind": "simulated", "name": name, "region": region,
                                            "gpu_model": model, "vram_gb": vram, "hourly_usd": price})
