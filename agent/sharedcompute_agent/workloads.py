"""Allowlisted workload templates the agent can execute.

The agent never runs code supplied by a customer. Each template here is fixed code that
takes small numeric parameters issued by the marketplace and returns output the server
can verify. Adding customer containers requires the isolation work described in
docs/ARCHITECTURE.md; it does not belong in this module.
"""
from __future__ import annotations

import hashlib
import time
from typing import Callable

LCG_A = 1664525
LCG_C = 1013904223


class Cancelled(Exception):
    """The job was cancelled by the marketplace or exceeded its time limit."""


ShouldStop = Callable[[], bool]


def hashchain(params: dict, should_stop: ShouldStop, gpu_slot: int | None = None) -> dict:
    digest = bytes.fromhex(params["seed"])
    iterations = int(params["iterations"])
    for i in range(iterations):
        if i % 10_000 == 0 and should_stop():
            raise Cancelled()
        digest = hashlib.sha256(digest).digest()
    return {"digest": digest.hex(), "iterations": iterations, "device": "cpu"}


def lcg_values(seed: int, count: int) -> list[int]:
    x = seed % 2**32
    out = [0] * count
    for i in range(count):
        x = (LCG_A * x + LCG_C) % 2**32
        out[i] = ((x >> 16) % 17) - 8
    return out


def seeded_matrices(seed: int, n: int) -> tuple[list[int], list[int]]:
    """A then B, row-major, from one LCG stream (same definition as the server's verifier)."""
    values = lcg_values(seed, 2 * n * n)
    return values[: n * n], values[n * n:]


def _torch():
    try:
        import torch  # type: ignore[import-not-found]
    except ImportError:
        return None
    return torch if torch.cuda.is_available() else None


def matmul(params: dict, should_stop: ShouldStop, gpu_slot: int | None = None) -> dict:
    torch = _torch()
    if torch is None:
        raise RuntimeError("PyTorch with CUDA is not available on this machine")
    device = torch.device(f"cuda:{gpu_slot or 0}")
    # Exact integer checksum on seeded matrices; values are small enough that FP32/TF32 is exact.
    n = int(params["n"])
    a_vals, b_vals = seeded_matrices(int(params["seed"]), n)
    a = torch.tensor(a_vals, dtype=torch.float32, device=device).reshape(n, n)
    b = torch.tensor(b_vals, dtype=torch.float32, device=device).reshape(n, n)
    checksum = int(round((a @ b).to(torch.float64).sum().item()))
    if should_stop():
        raise Cancelled()
    # Throughput measurement on random matrices. Reported, not verified by the server.
    bench_n, repeats = int(params["bench_n"]), int(params["repeats"])
    x = torch.randn(bench_n, bench_n, device=device)
    y = torch.randn(bench_n, bench_n, device=device)
    torch.cuda.synchronize(device)
    start = time.perf_counter()
    for _ in range(repeats):
        if should_stop():
            raise Cancelled()
        x @ y
    torch.cuda.synchronize(device)
    seconds = time.perf_counter() - start
    return {"checksum": checksum, "device": torch.cuda.get_device_name(device),
            "bench_seconds": round(seconds, 4),
            "tflops": round(2 * bench_n**3 * repeats / seconds / 1e12, 2)}


RUNNERS: dict[str, Callable[[dict, ShouldStop, int | None], dict]] = {
    "agent.hashchain": hashchain,
    "agent.matmul": matmul,
}


def capabilities() -> list[str]:
    caps = ["agent.hashchain"]
    if _torch() is not None:
        caps.append("agent.matmul")
    return caps
