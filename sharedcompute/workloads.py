"""Workload catalogue, hardware requirements and server-side result verification.

Two execution paths are kept strictly apart:

* ``simulated`` workloads are scheduled, timed and billed by the server's simulator.
  Nothing executes; they only run on simulated (self-reported) listings.
* ``agent`` workloads are allowlisted templates that the provider agent executes on a
  real machine. The customer cannot supply code. Each template returns output the
  server can check, so a job is only billed when its result verifies.

The agent implements the same templates in ``sharedcompute_agent.workloads``; the test
suite cross-checks both sides (LCG, matrix checksum, Merkle tree and challenge derivation).
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Mapping

LCG_A = 1664525
LCG_C = 1013904223


@dataclass(frozen=True)
class Workload:
    id: str
    label: str
    execution: str  # 'simulated' | 'agent'
    description: str
    device: str = "gpu"  # device type it runs on: 'gpu' | 'cpu'
    requires_cuda: bool = False
    min_compute_capability: str | None = None
    min_cuda_driver: int | None = None  # CUDA driver API version, e.g. 5050 = CUDA 5.5
    min_vram_mb: int = 0

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "execution": self.execution, "description": self.description,
                "device": self.device, "requires_cuda": self.requires_cuda,
                "min_compute_capability": self.min_compute_capability,
                "min_cuda_driver": self.min_cuda_driver, "min_vram_mb": self.min_vram_mb}


SIMULATED_NOTE = "Simulated. Scheduled and billed for the estimated minutes; nothing executes."

CATALOGUE: dict[str, Workload] = {w.id: w for w in [
    Workload("sim.image", "Image generation", "simulated", SIMULATED_NOTE),
    Workload("sim.inference", "LLM inference", "simulated", SIMULATED_NOTE),
    Workload("sim.finetune", "Fine-tuning", "simulated", SIMULATED_NOTE),
    Workload("sim.batch", "Batch compute", "simulated", SIMULATED_NOTE),
    Workload("agent.hashchain", "Pipeline check (CPU)", "agent",
             "Runs on a provider machine that offers its CPU. Computes a SHA-256 hash chain and returns the "
             "digest, which the server recomputes. Proves assignment, execution, verification and billing "
             "end to end. Does not use a GPU.", device="cpu"),
    Workload("agent.cuda_matmul", "CUDA integer matmul (GPU)", "agent",
             "Runs on an NVIDIA GPU offered by a provider agent: multiplies two seeded integer matrices with a "
             "CUDA kernel (driver JIT, no toolkit needed; Kepler sm_30 or newer). The server checks an exact "
             "checksum and recomputes randomly challenged rows against a Merkle commitment of the whole "
             "result. The GPU claim itself is attested by the agent and cross-checked, not proven.",
             device="gpu", requires_cuda=True, min_compute_capability="3.0", min_cuda_driver=5050,
             min_vram_mb=64),
]}

MATMUL_N = 256
MATMUL_REPEATS = 20
MATMUL_SAMPLES = 4


def get(workload_id: str) -> Workload | None:
    return CATALOGUE.get(workload_id)


# --- Hardware compatibility -------------------------------------------------------------

CC_RE = re.compile(r"^\d{1,2}\.\d$")


def parse_cc(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str) or not CC_RE.match(value):
        return None
    major, minor = value.split(".")
    return int(major), int(minor)


def cuda_version_label(version: int | None) -> str:
    if not version:
        return "unknown"
    return f"{version // 1000}.{(version % 1000) // 10}"


def incompatibilities(workload: Workload, device: Mapping) -> list[str]:
    """Why a device cannot run an agent workload; empty when it can.

    Checked by the server from the device report, independently of what the agent advertises.
    """
    reasons = []
    if device["device_type"] != workload.device:
        reasons.append(f"needs a {workload.device.upper()} device")
        return reasons
    if workload.requires_cuda and not device["cuda"]:
        reasons.append("needs an NVIDIA GPU with a working CUDA driver")
        return reasons
    if workload.min_compute_capability:
        have = parse_cc(device["compute_capability"])
        need = parse_cc(workload.min_compute_capability)
        if have is None:
            reasons.append("compute capability unknown")
        elif have < need:
            reasons.append(f"compute capability {device['compute_capability']} is below "
                           f"{workload.min_compute_capability}")
    if workload.min_cuda_driver:
        have_driver = device["cuda_driver_version"]
        if not have_driver:
            reasons.append("CUDA driver version unknown")
        elif have_driver < workload.min_cuda_driver:
            reasons.append(f"driver supports CUDA {cuda_version_label(have_driver)}; needs "
                           f"{cuda_version_label(workload.min_cuda_driver)} or newer")
    if workload.min_vram_mb and device["vram_mb"] < workload.min_vram_mb:
        reasons.append(f"{device['vram_mb']} MB VRAM is below {workload.min_vram_mb} MB")
    return reasons


# --- Parameters ---------------------------------------------------------------------------

def issue_params(workload: Workload, *, hashchain_iterations: int) -> dict:
    """Fresh parameters for each assignment, so a result cannot be replayed across attempts."""
    if workload.id == "agent.hashchain":
        return {"seed": secrets.token_hex(32), "iterations": hashchain_iterations}
    if workload.id == "agent.cuda_matmul":
        return {"seed": secrets.randbelow(2**32), "n": MATMUL_N, "repeats": MATMUL_REPEATS,
                "samples": MATMUL_SAMPLES, "nonce": secrets.token_hex(16)}
    return {}


# --- Shared deterministic primitives (mirrored in the agent) -----------------------------

def hashchain(seed_hex: str, iterations: int) -> str:
    digest = bytes.fromhex(seed_hex)
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()
    return digest.hex()


def lcg_values(seed: int, count: int) -> list[int]:
    """Deterministic integers in [-8, 8]; small enough that int32 products never overflow."""
    x = seed % 2**32
    out = [0] * count
    for i in range(count):
        x = (LCG_A * x + LCG_C) % 2**32
        out[i] = ((x >> 16) % 17) - 8
    return out


def matmul_checksum(seed: int, n: int) -> int:
    """sum(A @ B) for the seeded matrices, in O(n^2) via sum(A@B) = colsum(A) . rowsum(B)."""
    values = lcg_values(seed, 2 * n * n)
    a, b = values[: n * n], values[n * n:]
    col_a = [sum(a[i * n + k] for i in range(n)) for k in range(n)]
    return sum(col_a[k] * sum(b[k * n: (k + 1) * n]) for k in range(n))


def matmul_row(a: list[int], b: list[int], n: int, i: int) -> list[int]:
    row = [0] * n
    for k, a_ik in enumerate(a[i * n: (i + 1) * n]):
        if a_ik:
            b_row = b[k * n: (k + 1) * n]
            row = [r + a_ik * v for r, v in zip(row, b_row)]
    return row


def merkle_leaf(index: int, values: list[int]) -> bytes:
    body = b"".join(int(v).to_bytes(4, "little", signed=True) for v in values)
    return hashlib.sha256(b"sc-row" + index.to_bytes(4, "big") + body).digest()


def merkle_parent(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"sc-node" + left + right).digest()


def merkle_verify(index: int, leaf: bytes, proof: list[str], root_hex: str) -> bool:
    node = leaf
    for sibling_hex in proof:
        sibling = bytes.fromhex(sibling_hex)
        node = merkle_parent(node, sibling) if index % 2 == 0 else merkle_parent(sibling, node)
        index //= 2
    return node.hex() == root_hex


def challenge_rows(root_hex: str, nonce_hex: str, count: int, n: int) -> list[int]:
    """Rows to reveal, derived from the commitment so they cannot be chosen before computing it."""
    seed = hashlib.sha256(bytes.fromhex(root_hex) + bytes.fromhex(nonce_hex)).digest()
    rows: list[int] = []
    counter = 0
    while len(rows) < min(count, n):
        h = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        row = int.from_bytes(h[:4], "big") % n
        if row not in rows:
            rows.append(row)
        counter += 1
    return rows


# --- Verification -------------------------------------------------------------------------

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _verify_matmul(params: dict, output: dict) -> tuple[bool, str]:
    n, seed = params["n"], params["seed"]
    checksum = output.get("checksum")
    if not _is_int(checksum) or checksum != matmul_checksum(seed, n):
        return False, "Checksum does not match the server's computation"
    root = output.get("merkle_root")
    if not isinstance(root, str) or not HEX64.match(root):
        return False, "Missing or malformed Merkle root"
    samples = output.get("samples")
    expected_rows = challenge_rows(root, params["nonce"], params["samples"], n)
    if not isinstance(samples, list) or [s.get("row") if isinstance(s, dict) else None for s in samples] != expected_rows:
        return False, "Revealed rows do not match the challenge derived from the commitment"
    values = lcg_values(seed, 2 * n * n)
    a, b = values[: n * n], values[n * n:]
    for s in samples:
        row_values, proof = s.get("values"), s.get("proof")
        if (not isinstance(row_values, list) or len(row_values) != n or not all(_is_int(v) for v in row_values)
                or not isinstance(proof, list) or not all(isinstance(p, str) and HEX64.match(p) for p in proof)):
            return False, f"Row {s['row']} sample is malformed"
        if not merkle_verify(s["row"], merkle_leaf(s["row"], row_values), proof, root):
            return False, f"Row {s['row']} is not part of the committed result"
        if row_values != matmul_row(a, b, n, s["row"]):
            return False, f"Row {s['row']} is wrong"
    return True, f"Checksum exact; {len(samples)} challenged rows recomputed and proven against the commitment"


def verify(workload_id: str, params: dict, output: dict) -> tuple[bool, str]:
    """Check an agent's output against the issued parameters."""
    if not isinstance(output, dict):
        return False, "Output must be an object"
    if workload_id == "agent.hashchain":
        if output.get("digest") == hashchain(params["seed"], params["iterations"]):
            return True, "Digest matches server recomputation"
        return False, "Digest does not match server recomputation"
    if workload_id == "agent.cuda_matmul":
        return _verify_matmul(params, output)
    return False, "Workload has no verifier"
