"""Workload catalogue and server-side result verification.

Two execution paths are kept strictly apart:

* ``simulated`` workloads are scheduled, timed and billed by the server's simulator.
  Nothing executes; they only run on simulated (self-reported) listings.
* ``agent`` workloads are allowlisted templates that the provider agent executes on a
  real machine. The customer cannot supply code. Each template returns output the
  server can check, so a job is only billed when its result verifies.

The agent implements the same templates in ``sharedcompute_agent.workloads``; the test
suite cross-checks both sides.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

LCG_A = 1664525
LCG_C = 1013904223
MATMUL_VERIFY_N = 512


@dataclass(frozen=True)
class Workload:
    id: str
    label: str
    execution: str  # 'simulated' | 'agent'
    description: str
    requires_cuda: bool = False

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "execution": self.execution,
            "description": self.description,
            "requires_cuda": self.requires_cuda,
        }


CATALOGUE: dict[str, Workload] = {w.id: w for w in [
    Workload("sim.image", "Image generation", "simulated",
             "Simulated. Scheduled and billed for the estimated minutes; nothing executes."),
    Workload("sim.inference", "LLM inference", "simulated",
             "Simulated. Scheduled and billed for the estimated minutes; nothing executes."),
    Workload("sim.finetune", "Fine-tuning", "simulated",
             "Simulated. Scheduled and billed for the estimated minutes; nothing executes."),
    Workload("sim.batch", "Batch compute", "simulated",
             "Simulated. Scheduled and billed for the estimated minutes; nothing executes."),
    Workload("agent.hashchain", "Pipeline check (SHA-256 chain)", "agent",
             "Runs on a connected provider agent. Computes a SHA-256 hash chain on the CPU and "
             "returns the digest, which the server recomputes. Proves the assign, execute, "
             "verify and bill path end to end; it does not exercise the GPU."),
    Workload("agent.matmul", "CUDA matrix-multiply benchmark", "agent",
             "Runs on a connected agent with PyTorch and an NVIDIA CUDA GPU. Multiplies seeded "
             "integer matrices on the GPU; the server verifies an exact checksum. Throughput "
             "figures are reported by the provider and not independently verified.",
             requires_cuda=True),
]}


def get(workload_id: str) -> Workload | None:
    return CATALOGUE.get(workload_id)


def issue_params(workload: Workload, *, hashchain_iterations: int) -> dict:
    """Fresh parameters for each assignment, so a result cannot be replayed across attempts."""
    if workload.id == "agent.hashchain":
        return {"seed": secrets.token_hex(32), "iterations": hashchain_iterations}
    if workload.id == "agent.matmul":
        return {"seed": secrets.randbelow(2**32), "n": MATMUL_VERIFY_N,
                "bench_n": 4096, "repeats": 20}
    return {}


def hashchain(seed_hex: str, iterations: int) -> str:
    digest = bytes.fromhex(seed_hex)
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()
    return digest.hex()


def lcg_values(seed: int, count: int):
    """Deterministic integers in [-8, 8]. Small enough that FP32/TF32 matmul is exact."""
    x = seed % 2**32
    for _ in range(count):
        x = (LCG_A * x + LCG_C) % 2**32
        yield ((x >> 16) % 17) - 8


def matmul_checksum(seed: int, n: int) -> int:
    """sum(A @ B) for the seeded matrices, in O(n^2) via sum(A@B) = colsum(A) . rowsum(B)."""
    values = lcg_values(seed, 2 * n * n)
    col_a = [0] * n
    for _ in range(n):
        for k in range(n):
            col_a[k] += next(values)
    total = 0
    for k in range(n):
        row = 0
        for _ in range(n):
            row += next(values)
        total += col_a[k] * row
    return total


def verify(workload_id: str, params: dict, output: dict) -> tuple[bool, str]:
    """Check an agent's output against the issued parameters."""
    if not isinstance(output, dict):
        return False, "Output must be an object"
    if workload_id == "agent.hashchain":
        expected = hashchain(params["seed"], params["iterations"])
        if output.get("digest") == expected:
            return True, "Digest matches server recomputation"
        return False, "Digest does not match server recomputation"
    if workload_id == "agent.matmul":
        expected = matmul_checksum(params["seed"], params["n"])
        got = output.get("checksum")
        if isinstance(got, int) and not isinstance(got, bool) and got == expected:
            return True, "Checksum matches server computation"
        return False, "Checksum does not match server computation"
    return False, "Workload has no verifier"
