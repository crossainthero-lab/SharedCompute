"""Server side of the provider-agent protocol.

The agent always initiates (outbound HTTPS only, so it works behind NAT):

    hello      report version, platform and devices (GPUs, optionally the CPU) with opt-in status
    heartbeat  liveness + GPU metrics; the reply lists jobs the agent must stop
    claim      take the next job assigned to this node (assigned -> running)
    complete   submit output; the server verifies it and records execution evidence before billing
    fail       report an execution failure; `code: "incompatible"` is a graceful hardware rejection

Hardware reports are self-attested by the agent. They come from NVML / nvidia-smi / the CUDA driver /
the OS, which beats a typed-in listing, but a modified agent could lie. The server therefore re-checks
compatibility itself and records how much of each execution it could actually verify.
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
DETECTION_SOURCES = {"nvml", "nvidia-smi", "cuda", "system_profiler", "lspci", "os"}
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16,64}$")
MAX_DEVICES = 16
GPU_CLAIM_NOTE = ("GPU execution is attested by the provider agent and cross-checked against the registered "
                  "device and the server's timing; it is not cryptographically proven.")


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


def _opt_int(value: object, field: str, low: int, high: int) -> int | None:
    if value is None:
        return None
    return v.integer(value, field, field, low, high)


def _clean_device(raw: object, index: int) -> dict:
    f = f"gpus[{index}]"
    if not isinstance(raw, dict):
        raise InvalidInput("Each device report must be an object", field=f)
    fingerprint = raw.get("fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not FINGERPRINT_RE.match(fingerprint)):
        raise InvalidInput("Device fingerprint must be lowercase hex", field=f"{f}.fingerprint")
    if raw.get("detection") not in DETECTION_SOURCES:
        raise InvalidInput("Unknown device detection source", field=f"{f}.detection")
    device_type = raw.get("device_type", "gpu")
    if device_type not in ("gpu", "cpu"):
        raise InvalidInput("device_type must be gpu or cpu", field=f"{f}.device_type")
    cc = _opt_text(raw.get("compute_capability"), f"{f}.compute_capability", 10)
    if cc is not None and workloads.parse_cc(cc) is None:
        raise InvalidInput("compute_capability must look like 6.1", field=f"{f}.compute_capability")
    advertised = raw.get("workloads", [])
    if not isinstance(advertised, list):
        raise InvalidInput("workloads must be a list", field=f"{f}.workloads")
    return {
        "slot": v.integer(raw.get("slot"), f"{f}.slot", "Device slot", 0, 63),
        "device_type": device_type,
        "fingerprint": fingerprint,
        "vendor": v.text(raw.get("vendor"), f"{f}.vendor", "Vendor", max_len=40),
        "model": v.text(raw.get("model"), f"{f}.model", "Model", max_len=120),
        "vram_mb": v.integer(raw.get("vram_mb"), f"{f}.vram_mb", "VRAM", 0, 1_048_576),
        "vram_free_mb": _opt_int(raw.get("vram_free_mb"), f"{f}.vram_free_mb", 0, 1_048_576),
        "cuda": bool(raw.get("cuda", False)),
        "compute_capability": cc,
        "cuda_driver_version": _opt_int(raw.get("cuda_driver_version"), f"{f}.cuda_driver_version", 0, 99_999),
        "driver_version": _opt_text(raw.get("driver_version"), f"{f}.driver_version", 40),
        "integrated": bool(raw.get("integrated", False)),
        "display_active": None if raw.get("display_active") is None else bool(raw.get("display_active")),
        "detection": raw["detection"],
        # Opt-in happens on the provider's machine; an agent that does not say so offers nothing.
        "offered": raw.get("offered") is True,
        "offer_note": _opt_text(raw.get("offer_note"), f"{f}.offer_note", 300),
        "workloads": sorted({w for w in advertised if isinstance(w, str) and (wl := workloads.get(w))
                             and wl.execution == "agent" and wl.device == device_type}),
    }


def hello(conn: sqlite3.Connection, settings: Settings, node: sqlite3.Row, body: dict) -> dict:
    now = settings.now()
    version = v.text(body.get("agent_version"), "agent_version", "Agent version", max_len=40)
    platform = _opt_text(body.get("platform"), "platform", 120)
    concurrency = v.integer(body.get("max_concurrency", 1), "max_concurrency", "Max concurrency", 1, 8)
    raw = body.get("gpus", [])
    if not isinstance(raw, list) or len(raw) > MAX_DEVICES:
        raise InvalidInput(f"gpus must be a list of at most {MAX_DEVICES} devices", field="gpus")
    reported = [_clean_device(d, i) for i, d in enumerate(raw)]

    existing = conn.execute("SELECT * FROM gpus WHERE node_id = ?", (node["id"],)).fetchall()
    by_fp = {(g["device_type"], g["fingerprint"]): g for g in existing if g["fingerprint"]}
    by_slot = {(g["device_type"], g["slot"]): g for g in existing if not g["fingerprint"]}
    seen: set[int] = set()
    for d in reported:
        key = (d["device_type"], d["fingerprint"]) if d["fingerprint"] else (d["device_type"], d["slot"])
        match = (by_fp if d["fingerprint"] else by_slot).get(key)
        values = (d["slot"], d["device_type"], d["vendor"], d["model"], d["vram_mb"], d["vram_free_mb"], int(d["cuda"]),
                  d["compute_capability"], d["cuda_driver_version"], d["driver_version"], int(d["integrated"]),
                  None if d["display_active"] is None else int(d["display_active"]), d["detection"],
                  int(d["offered"]), d["offer_note"], json.dumps(d["workloads"]))
        if match is not None and match["id"] not in seen:
            conn.execute(
                """UPDATE gpus SET slot = ?, device_type = ?, vendor = ?, model = ?, vram_mb = ?, vram_free_mb = ?,
                          cuda = ?, compute_capability = ?, cuda_driver_version = ?, driver_version = ?,
                          integrated = ?, display_active = ?, detection = ?, offered = ?, offer_note = ?,
                          capabilities = ?, present = 1 WHERE id = ?""", (*values, match["id"]))
            device_id = match["id"]
            if match["offered"] and not d["offered"]:
                jobs.interrupt_active(conn, settings, "Provider withdrew the device on the machine", gpu_id=device_id)
        else:
            device_id = conn.execute(
                """INSERT INTO gpus(slot, device_type, vendor, model, vram_mb, vram_free_mb, cuda, compute_capability,
                                    cuda_driver_version, driver_version, integrated, display_active, detection,
                                    offered, offer_note, capabilities, node_id, fingerprint, hourly_cents, enabled,
                                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*values, node["id"], d["fingerprint"], node["default_hourly_cents"], int(d["offered"]), now),
            ).lastrowid
        seen.add(device_id)
    for g in existing:
        if g["id"] not in seen and g["present"]:
            conn.execute("UPDATE gpus SET present = 0 WHERE id = ?", (g["id"],))
            jobs.interrupt_active(conn, settings, "Device no longer reported by the agent", gpu_id=g["id"])

    offered_caps = sorted({w for d in reported if d["offered"] for w in d["workloads"]})
    conn.execute(
        """UPDATE nodes SET agent_version = ?, platform = ?, capabilities = ?, max_concurrency = ?, last_seen_at = ?
           WHERE id = ?""",
        (version, platform, json.dumps(offered_caps), concurrency, now, node["id"]),
    )
    rows = conn.execute("SELECT * FROM gpus WHERE node_id = ? AND present = 1 ORDER BY device_type DESC, slot",
                        (node["id"],)).fetchall()
    devices = []
    for g in rows:
        accepted = [w for w in json.loads(g["capabilities"])
                    if not workloads.incompatibilities(workloads.get(w), g)]
        rejected = {w: workloads.incompatibilities(workloads.get(w), g) for w in json.loads(g["capabilities"])
                    if w not in accepted}
        devices.append({"id": g["id"], "slot": g["slot"], "device_type": g["device_type"], "model": g["model"],
                        "offered": bool(g["offered"]), "enabled": bool(g["enabled"]), "hourly_cents": g["hourly_cents"],
                        "workloads": accepted, "rejected_workloads": rejected})
    return {"node_id": node["id"], "node_name": node["name"], "capabilities": offered_caps,
            "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS, "gpus": devices}


def heartbeat(conn: sqlite3.Connection, settings: Settings, node: sqlite3.Row, body: dict) -> dict:
    now = settings.now()
    conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (now, node["id"]))
    metrics = body.get("gpu_metrics", [])
    if isinstance(metrics, list):
        for m in metrics[:MAX_DEVICES]:
            if not isinstance(m, dict) or not isinstance(m.get("slot"), int):
                continue
            conn.execute(
                """UPDATE gpus SET util_percent = ?, mem_used_mb = ?, temperature_c = ?, metrics_at = ?
                   WHERE node_id = ? AND slot = ? AND device_type = 'gpu' AND present = 1""",
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
        """SELECT j.*, g.slot, g.device_type, g.fingerprint FROM jobs j JOIN gpus g ON g.id = j.gpu_id
           WHERE j.node_id = ? AND j.status = 'assigned' ORDER BY j.assigned_at, j.id LIMIT 1""", (node["id"],)
    ).fetchone()
    if job is None:
        return {"job": None}
    conn.execute("UPDATE jobs SET status = 'running', started_at = ?, lease_expires_at = NULL WHERE id = ?",
                 (now, job["id"]))
    jobs.event(conn, job["id"], "started", now, "Provider agent claimed the job", node_id=node["id"])
    return {"job": {"id": job["id"], "workload": job["workload"], "params": json.loads(job["params"]),
                    "device": {"type": job["device_type"], "slot": job["slot"], "fingerprint": job["fingerprint"]},
                    "gpu_slot": job["slot"], "time_limit_seconds": job["est_minutes"] * 60}}


def _running_job(conn: sqlite3.Connection, node: sqlite3.Row, job_id: int) -> sqlite3.Row:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None or job["node_id"] != node["id"]:
        raise NotFound("Job not found for this node")
    if job["status"] != "running":
        raise Conflict(f"Job is {job['status']}; stop work on it")
    return job


def execution_evidence(workload: workloads.Workload, device: sqlite3.Row, params: dict, output: dict,
                       verified: bool, observed_seconds: float) -> dict:
    """What the server can say about where and how a job ran, separated from what the agent claims."""
    claimed = output.get("device") if isinstance(output.get("device"), dict) else {}
    claimed_type = claimed.get("type") if claimed.get("type") in ("gpu", "cpu") else "unknown"
    evidence = {"result_verified": verified, "claimed_device": claimed_type,
                "backend": claimed.get("backend") if isinstance(claimed.get("backend"), str) else None}
    if workload.device == "cpu":
        evidence["level"] = "cpu_verified" if verified and claimed_type == "cpu" else (
            "failed_verification" if not verified else "inconsistent")
        return evidence

    identity = {}
    if device["fingerprint"]:
        identity["fingerprint_matches"] = claimed.get("fingerprint") == device["fingerprint"]
    if device["compute_capability"]:
        identity["compute_capability_matches"] = claimed.get("compute_capability") == device["compute_capability"]
    identity_ok = bool(identity) and all(identity.values())
    kernel_ms = output.get("kernel_ms")
    repeats = params.get("repeats", 1)
    timing_ok = (isinstance(kernel_ms, (int, float)) and not isinstance(kernel_ms, bool) and kernel_ms > 0
                 and kernel_ms * repeats / 1000 <= observed_seconds + 1.0)
    evidence.update({"device_identity": identity, "timing": {
        "kernel_ms": kernel_ms if isinstance(kernel_ms, (int, float)) else None, "repeats": repeats,
        "server_observed_seconds": round(observed_seconds, 3), "consistent": timing_ok}})
    if not verified:
        evidence["level"] = "failed_verification"
    elif claimed_type == "cpu":
        evidence["level"] = "cpu_fallback"
    elif claimed_type == "gpu" and identity_ok and timing_ok:
        evidence["level"] = "gpu_attested"
        evidence["note"] = GPU_CLAIM_NOTE
    else:
        evidence["level"] = "inconsistent"
    return evidence


def complete(conn: sqlite3.Connection, settings: Settings, node: sqlite3.Row, job_id: int, body: dict) -> dict:
    job = _running_job(conn, node, job_id)
    output = body.get("output")
    if not isinstance(output, dict):
        raise InvalidInput("output must be an object", field="output")
    workload = workloads.get(job["workload"])
    device = conn.execute("SELECT * FROM gpus WHERE id = ?", (job["gpu_id"],)).fetchone()
    params = json.loads(job["params"])
    reported_runtime = _num(body.get("runtime_seconds"), 0, 10**7)
    observed = max(0.0, settings.now() - job["started_at"])

    ok, detail = workloads.verify(job["workload"], params, output)
    evidence = execution_evidence(workload, device, params, output, ok, observed)
    executed_on = evidence["claimed_device"] if evidence["claimed_device"] in ("gpu", "cpu") else None
    # Keep the stored result small: drop the bulky revealed rows once verified.
    stored = {k: val for k, val in output.items() if k != "samples"}
    result = {"output": stored, "verification": detail, "agent_reported_runtime_seconds": reported_runtime}

    failure = None
    if not ok:
        failure = f"Result failed verification: {detail}"
    elif evidence["level"] == "cpu_fallback":
        failure = "Executed on the CPU fallback; this workload requires a GPU. Not billed."
    elif evidence["level"] == "inconsistent":
        failure = "Execution evidence is inconsistent with the assigned device. Not billed."
    if failure:
        jobs.fail(conn, settings, job, failure, node_id=node["id"], verified=ok, result=result,
                  executed_on=executed_on, evidence=evidence)
        return {"status": "failed", "verified": ok, "detail": failure, "evidence": evidence["level"]}
    # Bill server-observed time between claim and completion, never the agent's own figure.
    jobs.complete(conn, settings, job, billed_seconds=observed,
                  cost_cents=money.metered_cents(job["hourly_cents"], observed), result=result, verified=True,
                  executed_on=executed_on, evidence=evidence)
    return {"status": "completed", "verified": True, "detail": detail, "evidence": evidence["level"]}


def fail(conn: sqlite3.Connection, settings: Settings, node: sqlite3.Row, job_id: int, body: dict) -> dict:
    job = _running_job(conn, node, job_id)
    reason = v.text(body.get("reason", "Agent reported a failure"), "reason", "Reason", max_len=300)
    if body.get("code") == "incompatible":
        # Graceful rejection: the device cannot run this workload after all (e.g. not enough free VRAM,
        # CUDA unavailable). Stop offering it for this workload until the agent re-advertises, and requeue
        # without costing the customer an attempt.
        device = conn.execute("SELECT capabilities FROM gpus WHERE id = ?", (job["gpu_id"],)).fetchone()
        caps = [w for w in json.loads(device["capabilities"]) if w != job["workload"]]
        conn.execute("UPDATE gpus SET capabilities = ? WHERE id = ?", (json.dumps(caps), job["gpu_id"]))
        status = jobs.end_attempt(conn, settings, job, f"Device declined: {reason}", retryable=True,
                                  count_attempt=False)
        return {"status": status}
    retryable = body.get("retryable", False) is True
    status = jobs.end_attempt(conn, settings, job, f"Agent: {reason}", retryable=retryable)
    return {"status": status}
