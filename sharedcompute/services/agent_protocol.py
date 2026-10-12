"""Server side of the provider-agent protocol.

The agent always initiates (outbound HTTPS only, so it works behind NAT):

    hello      report version, platform, capabilities and detected GPUs
    heartbeat  liveness + GPU metrics; the reply lists jobs the agent must stop
    claim      take the next job assigned to this node (assigned -> running)
    complete   submit output; the server verifies it before anything is billed
    fail       report an execution failure, optionally retryable

Hardware reports are self-attested by the agent. They are far better than a typed-in
listing (they come from NVML / nvidia-smi / the OS) but are not cryptographically attested.
"""
from __future__ import annotations

import json
import re
import sqlite3

from .. import money, workloads
from .. import validation as v
from ..config import Settings
from ..errors import Conflict, InvalidInput, NotFound, Unauthorized
from ..security import NODE_PREFIX, token_hash
from . import jobs

HEARTBEAT_INTERVAL_SECONDS = 10
DETECTION_SOURCES = {"nvml", "nvidia-smi", "system_profiler", "lspci"}
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16,64}$")
MAX_GPUS = 16


def authenticate(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row:
    if not token or not token.startswith(NODE_PREFIX):
        raise Unauthorized("Missing or malformed node token")
    node = conn.execute("SELECT * FROM nodes WHERE token_hash = ? AND retired = 0 AND kind = 'agent'",
                        (token_hash(token),)).fetchone()
    if node is None:
        raise Unauthorized("Unknown or revoked node token")
    return node


def _opt_text(value: object, field: str, max_len: int = 80) -> str | None:
    if value in (None, ""):
        return None
    return v.text(value, field, field, max_len=max_len)


def _clean_gpu(raw: object, index: int) -> dict:
    if not isinstance(raw, dict):
        raise InvalidInput("Each GPU report must be an object", field=f"gpus[{index}]")
    fingerprint = raw.get("fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not FINGERPRINT_RE.match(fingerprint)):
        raise InvalidInput("GPU fingerprint must be lowercase hex", field=f"gpus[{index}].fingerprint")
    detection = raw.get("detection")
    if detection not in DETECTION_SOURCES:
        raise InvalidInput("Unknown GPU detection source", field=f"gpus[{index}].detection")
    return {
        "slot": v.integer(raw.get("slot"), f"gpus[{index}].slot", "GPU slot", 0, 63),
        "fingerprint": fingerprint,
        "vendor": v.text(raw.get("vendor"), f"gpus[{index}].vendor", "GPU vendor", max_len=40),
        "model": v.text(raw.get("model"), f"gpus[{index}].model", "GPU model", max_len=80),
        "vram_mb": v.integer(raw.get("vram_mb"), f"gpus[{index}].vram_mb", "VRAM", 0, 1_048_576),
        "cuda": bool(raw.get("cuda", False)),
        "compute_capability": _opt_text(raw.get("compute_capability"), "compute_capability", 10),
        "driver_version": _opt_text(raw.get("driver_version"), "driver_version", 40),
        "integrated": bool(raw.get("integrated", False)),
        "detection": detection,
    }


def hello(conn: sqlite3.Connection, settings: Settings, node: sqlite3.Row, body: dict) -> dict:
    now = settings.now()
    version = v.text(body.get("agent_version"), "agent_version", "Agent version", max_len=40)
    platform = _opt_text(body.get("platform"), "platform", 120)
    caps = body.get("capabilities", [])
    if not isinstance(caps, list):
        raise InvalidInput("capabilities must be a list", field="capabilities")
    # Only allowlisted agent workloads can be advertised.
    capabilities = sorted({c for c in caps if isinstance(c, str) and (w := workloads.get(c)) and w.execution == "agent"})
    concurrency = v.integer(body.get("max_concurrency", 1), "max_concurrency", "Max concurrency", 1, 8)
    raw_gpus = body.get("gpus", [])
    if not isinstance(raw_gpus, list) or len(raw_gpus) > MAX_GPUS:
        raise InvalidInput(f"gpus must be a list of at most {MAX_GPUS} devices", field="gpus")
    reported = [_clean_gpu(g, i) for i, g in enumerate(raw_gpus)]

    existing = conn.execute("SELECT * FROM gpus WHERE node_id = ?", (node["id"],)).fetchall()
    by_fp = {g["fingerprint"]: g for g in existing if g["fingerprint"]}
    by_slot = {g["slot"]: g for g in existing if not g["fingerprint"]}
    seen: set[int] = set()
    for g in reported:
        match = by_fp.get(g["fingerprint"]) if g["fingerprint"] else by_slot.get(g["slot"])
        if match is not None and match["id"] not in seen:
            conn.execute(
                """UPDATE gpus SET slot = ?, vendor = ?, model = ?, vram_mb = ?, cuda = ?, compute_capability = ?,
                          driver_version = ?, integrated = ?, detection = ?, present = 1 WHERE id = ?""",
                (g["slot"], g["vendor"], g["model"], g["vram_mb"], int(g["cuda"]), g["compute_capability"],
                 g["driver_version"], int(g["integrated"]), g["detection"], match["id"]),
            )
            seen.add(match["id"])
        else:
            gpu_id = conn.execute(
                """INSERT INTO gpus(node_id, slot, fingerprint, vendor, model, vram_mb, cuda, compute_capability,
                                    driver_version, integrated, detection, hourly_cents, enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (node["id"], g["slot"], g["fingerprint"], g["vendor"], g["model"], g["vram_mb"], int(g["cuda"]),
                 g["compute_capability"], g["driver_version"], int(g["integrated"]), g["detection"],
                 node["default_hourly_cents"], int(not g["integrated"]), now),  # integrated GPUs start paused
            ).lastrowid
            seen.add(gpu_id)
    for g in existing:
        if g["id"] not in seen and g["present"]:
            conn.execute("UPDATE gpus SET present = 0 WHERE id = ?", (g["id"],))
            jobs.interrupt_active(conn, settings, "GPU no longer reported by the agent", gpu_id=g["id"])

    conn.execute(
        """UPDATE nodes SET agent_version = ?, platform = ?, capabilities = ?, max_concurrency = ?, last_seen_at = ?
           WHERE id = ?""",
        (version, platform, json.dumps(capabilities), concurrency, now, node["id"]),
    )
    gpus = conn.execute("SELECT id, slot, model, enabled, hourly_cents FROM gpus WHERE node_id = ? AND present = 1 "
                        "ORDER BY slot", (node["id"],)).fetchall()
    return {"node_id": node["id"], "node_name": node["name"], "capabilities": capabilities,
            "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
            "gpus": [{"id": g["id"], "slot": g["slot"], "model": g["model"], "enabled": bool(g["enabled"]),
                      "hourly_cents": g["hourly_cents"]} for g in gpus]}


def heartbeat(conn: sqlite3.Connection, settings: Settings, node: sqlite3.Row, body: dict) -> dict:
    now = settings.now()
    conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (now, node["id"]))
    metrics = body.get("gpu_metrics", [])
    if isinstance(metrics, list):
        for m in metrics[:MAX_GPUS]:
            if not isinstance(m, dict) or not isinstance(m.get("slot"), int):
                continue
            conn.execute(
                """UPDATE gpus SET util_percent = ?, mem_used_mb = ?, temperature_c = ?, metrics_at = ?
                   WHERE node_id = ? AND slot = ? AND present = 1""",
                (_num(m.get("util_percent"), 0, 100), _num(m.get("mem_used_mb"), 0, 1_048_576, int),
                 _num(m.get("temperature_c"), -50, 150), now, node["id"], m["slot"]),
            )
    running = body.get("running_job_ids", [])
    if not isinstance(running, list):
        raise InvalidInput("running_job_ids must be a list", field="running_job_ids")
    running_ids = [j for j in running if isinstance(j, int) and not isinstance(j, bool)][:64]
    still_ours = {r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE node_id = ? AND status = 'running'", (node["id"],))}
    return {"cancel_job_ids": [j for j in running_ids if j not in still_ours],
            "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS}


def _num(value: object, low: float, high: float, cast=float):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return cast(min(max(value, low), high))


def claim(conn: sqlite3.Connection, settings: Settings, node: sqlite3.Row) -> dict:
    now = settings.now()
    conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (now, node["id"]))
    if not node["enabled"]:
        return {"job": None}
    job = conn.execute(
        """SELECT j.*, g.slot FROM jobs j JOIN gpus g ON g.id = j.gpu_id
           WHERE j.node_id = ? AND j.status = 'assigned' ORDER BY j.assigned_at, j.id LIMIT 1""", (node["id"],)
    ).fetchone()
    if job is None:
        return {"job": None}
    conn.execute("UPDATE jobs SET status = 'running', started_at = ?, lease_expires_at = NULL WHERE id = ?",
                 (now, job["id"]))
    jobs.event(conn, job["id"], "started", now, "Provider agent claimed the job", node_id=node["id"])
    return {"job": {"id": job["id"], "workload": job["workload"], "params": json.loads(job["params"]),
                    "gpu_slot": job["slot"],
                    "time_limit_seconds": job["est_minutes"] * 60}}


def _running_job(conn: sqlite3.Connection, node: sqlite3.Row, job_id: int) -> sqlite3.Row:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None or job["node_id"] != node["id"]:
        raise NotFound("Job not found for this node")
    if job["status"] != "running":
        raise Conflict(f"Job is {job['status']}; stop work on it")
    return job


def complete(conn: sqlite3.Connection, settings: Settings, node: sqlite3.Row, job_id: int, body: dict) -> dict:
    job = _running_job(conn, node, job_id)
    output = body.get("output")
    if not isinstance(output, dict):
        raise InvalidInput("output must be an object", field="output")
    reported_runtime = _num(body.get("runtime_seconds"), 0, 10**7)
    ok, detail = workloads.verify(job["workload"], json.loads(job["params"]), output)
    result = {"output": output, "verification": detail, "agent_reported_runtime_seconds": reported_runtime}
    if not ok:
        jobs.fail(conn, settings, job, f"Result failed verification: {detail}", node_id=node["id"],
                  verified=False, result=result)
        return {"status": "failed", "verified": False, "detail": detail}
    # Bill server-observed time between claim and completion, never the agent's own figure.
    billed = max(0.0, settings.now() - job["started_at"])
    jobs.complete(conn, settings, job, billed_seconds=billed,
                  cost_cents=money.metered_cents(job["hourly_cents"], billed), result=result, verified=True)
    return {"status": "completed", "verified": True, "detail": detail}


def fail(conn: sqlite3.Connection, settings: Settings, node: sqlite3.Row, job_id: int, body: dict) -> dict:
    job = _running_job(conn, node, job_id)
    reason = v.text(body.get("reason", "Agent reported a failure"), "reason", "Reason", max_len=300)
    retryable = body.get("retryable", False) is True
    status = jobs.end_attempt(conn, settings, job, f"Agent: {reason}", retryable=retryable)
    return {"status": status}

