"""Allowlisted workload templates the agent can execute.

The agent never runs code supplied by a customer. Each template is fixed code that takes small numeric
parameters issued by the marketplace and returns output the server can verify. Customer containers
need the isolation work in docs/ARCHITECTURE.md §7 first; they do not belong in this module.

Every output carries a `device` block saying where it really ran. The CPU fallback for the GPU matmul
is off unless the provider passes --allow-cpu-fallback, and is always reported as `type: "cpu"`;
the marketplace never bills it as GPU work.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import cuda
from .errors import Cancelled, Incompatible

LCG_A = 1664525
LCG_C = 1013904223

# Requirements mirrored from the server catalogue (tests assert they match).
REQUIREMENTS = {
    "agent.hashchain": {"device": "cpu"},
    "agent.cuda_matmul": {"device": "gpu", "min_compute_capability": (3, 0), "min_cuda_driver": 5050,
                          "min_vram_mb": 64},
}

ShouldStop = Callable[[], bool]


@dataclass
class RunOptions:
    allow_cpu_fallback: bool = False
    driver_factory: Callable[[], "cuda.Driver"] = cuda.Driver
    log: List[str] = field(default_factory=list)


def _cc(value: Optional[str]) -> Optional[Tuple[int, int]]:
    try:
        major, minor = (value or "").split(".")
        return int(major), int(minor)
    except ValueError:
        return None


def device_workloads(device) -> Tuple[List[str], List[str]]:
    """(workloads this device can run, reasons it cannot run the others)."""
    ok, reasons = [], []
    for workload, req in REQUIREMENTS.items():
        if req["device"] != device.device_type:
            continue
        why = []
        if workload == "agent.cuda_matmul":
            if device.vendor != "NVIDIA":
                why.append("not an NVIDIA GPU")
            elif not device.cuda:
                why.append("the CUDA driver could not open this GPU")
            else:
                cc = _cc(device.compute_capability)
                if cc is None or cc < req["min_compute_capability"]:
                    why.append(f"compute capability {device.compute_capability or 'unknown'} is below 3.0")
                if (device.cuda_driver_version or 0) < req["min_cuda_driver"]:
                    why.append("driver older than CUDA 5.5")
                if device.vram_mb < req["min_vram_mb"]:
                    why.append(f"{device.vram_mb} MB VRAM is below {req['min_vram_mb']} MB")
        if why:
            reasons.append(f"{workload}: {'; '.join(why)}")
        else:
            ok.append(workload)
    return ok, reasons


# --- Pipeline check (CPU) ------------------------------------------------------------------

def hashchain(params: dict, should_stop: ShouldStop, device=None, options: Optional[RunOptions] = None) -> dict:
    digest = bytes.fromhex(params["seed"])
    iterations = int(params["iterations"])
    for i in range(iterations):
        if i % 10_000 == 0 and should_stop():
            raise Cancelled()
        digest = hashlib.sha256(digest).digest()
    return {"digest": digest.hex(), "iterations": iterations, "device": {"type": "cpu", "backend": "python-hashlib"}}


# --- Integer matmul (GPU, with an explicit CPU fallback) -----------------------------------

def lcg_values(seed: int, count: int) -> List[int]:
    x = seed % 2**32
    out = [0] * count
    for i in range(count):
        x = (LCG_A * x + LCG_C) % 2**32
        out[i] = ((x >> 16) % 17) - 8
    return out


def seeded_matrices(seed: int, n: int) -> Tuple[List[int], List[int]]:
    """A then B, row-major, from one LCG stream (same definition as the server's verifier)."""
    values = lcg_values(seed, 2 * n * n)
    return values[: n * n], values[n * n:]


def cpu_matmul(a: List[int], b: List[int], n: int, should_stop: ShouldStop = lambda: False) -> List[int]:
    c: List[int] = []
    for i in range(n):
        if should_stop():
            raise Cancelled()
        row = [0] * n
        for k, a_ik in enumerate(a[i * n: (i + 1) * n]):
            if a_ik:
                b_row = b[k * n: (k + 1) * n]
                row = [r + a_ik * v for r, v in zip(row, b_row)]
        c.extend(row)
    return c


def merkle_leaf(index: int, values: List[int]) -> bytes:
    body = b"".join(int(v).to_bytes(4, "little", signed=True) for v in values)
    return hashlib.sha256(b"sc-row" + index.to_bytes(4, "big") + body).digest()


def merkle_parent(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"sc-node" + left + right).digest()


def merkle_tree(leaves: List[bytes]) -> List[List[bytes]]:
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        level = levels[-1]
        if len(level) % 2:
            level = level + [level[-1]]
        levels.append([merkle_parent(level[i], level[i + 1]) for i in range(0, len(level), 2)])
    return levels


def merkle_proof(levels: List[List[bytes]], index: int) -> List[str]:
    proof = []
    for level in levels[:-1]:
        sibling = index ^ 1
        proof.append((level[sibling] if sibling < len(level) else level[index]).hex())
        index //= 2
    return proof


def challenge_rows(root_hex: str, nonce_hex: str, count: int, n: int) -> List[int]:
    seed = hashlib.sha256(bytes.fromhex(root_hex) + bytes.fromhex(nonce_hex)).digest()
    rows: List[int] = []
    counter = 0
    while len(rows) < min(count, n):
        h = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        row = int.from_bytes(h[:4], "big") % n
        if row not in rows:
            rows.append(row)
        counter += 1
    return rows


def commit(c: List[int], n: int, nonce: str, samples: int) -> dict:
    """Merkle-commit to every row, then reveal the rows the commitment itself selects."""
    rows = [c[i * n: (i + 1) * n] for i in range(n)]
    levels = merkle_tree([merkle_leaf(i, r) for i, r in enumerate(rows)])
    root = levels[-1][0].hex()
    picked = challenge_rows(root, nonce, samples, n)
    return {"checksum": sum(c), "merkle_root": root,
            "samples": [{"row": i, "values": rows[i], "proof": merkle_proof(levels, i)} for i in picked]}


def cuda_matmul(params: dict, should_stop: ShouldStop, device, options: Optional[RunOptions] = None) -> dict:
    options = options or RunOptions()
    n, repeats = int(params["n"]), int(params["repeats"])
    a, b = seeded_matrices(int(params["seed"]), n)
    fallback_reason = None
    if device is not None and device.device_type == "gpu" and device.cuda and device.cuda_ordinal is not None:
        try:
            c, kernel_ms = cuda.run_matmul_i32(options.driver_factory(), device.cuda_ordinal, a, b, n, repeats,
                                               should_stop)
            info = {"type": "gpu", "backend": "cuda-driver-ptx", "name": device.model,
                    "fingerprint": device.fingerprint, "compute_capability": device.compute_capability}
            return {"device": info, "kernel_ms": round(kernel_ms, 4), "repeats": repeats, "n": n,
                    **commit(c, n, params["nonce"], int(params["samples"]))}
        except (cuda.CudaError, cuda.CudaUnavailable) as exc:
            if not options.allow_cpu_fallback:
                raise
            fallback_reason = f"CUDA failed: {exc}"
    else:
        fallback_reason = "no CUDA-usable GPU for this job"
        if not options.allow_cpu_fallback:
            raise Incompatible(fallback_reason)
    options.log.append(f"CPU fallback: {fallback_reason}")
    start = time.perf_counter()
    c = cpu_matmul(a, b, n, should_stop)
    elapsed_ms = (time.perf_counter() - start) * 1000
    info = {"type": "cpu", "backend": "python-cpu-fallback", "fallback_reason": fallback_reason}
    return {"device": info, "kernel_ms": round(elapsed_ms, 2), "repeats": 1, "n": n,
            **commit(c, n, params["nonce"], int(params["samples"]))}


RUNNERS: Dict[str, Callable] = {
    "agent.hashchain": hashchain,
    "agent.cuda_matmul": cuda_matmul,
}
