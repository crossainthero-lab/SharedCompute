"""Hardware diagnostics for providers: what was detected, what each device can run, and an optional
self-test that executes the real GPU kernel and checks it against the CPU reference."""
from __future__ import annotations

import platform
import sys
import time
from typing import Callable, List, Optional

from . import __version__, cuda, gpu, workloads
from .policy import OfferPolicy

SELFTEST_N = 64


def cuda_version_label(version: Optional[int]) -> str:
    return f"{version // 1000}.{(version % 1000) // 10}" if version else "unknown"


def selftest_gpu(device, driver_factory: Callable = cuda.Driver, n: int = SELFTEST_N) -> dict:
    """Run the production kernel on one GPU and compare every element with the CPU result."""
    a, b = workloads.seeded_matrices(20251012, n)
    started = time.perf_counter()
    try:
        c, kernel_ms = cuda.run_matmul_i32(driver_factory(), device.cuda_ordinal, a, b, n, 3, lambda: False)
    except Exception as exc:  # report every failure mode verbatim; this is a diagnostic
        return {"slot": device.slot, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    expected = workloads.cpu_matmul(a, b, n)
    mismatches = sum(1 for x, y in zip(c, expected) if x != y)
    return {"slot": device.slot, "ok": mismatches == 0 and len(c) == len(expected), "n": n,
            "mismatched_elements": mismatches, "kernel_ms": round(kernel_ms, 4),
            "wall_seconds": round(time.perf_counter() - started, 3), "backend": "cuda-driver-ptx"}


def diagnose(policy: OfferPolicy, *, selftest: bool = False, detector: Callable[[], list] = gpu.detect,
             cpu_detector: Callable[[], object] = gpu.cpu_device, cuda_probe: Callable[[], dict] = cuda.probe,
             driver_factory: Callable = cuda.Driver) -> dict:
    probe = cuda_probe()
    devices = list(detector()) + [cpu_detector()]
    warnings = policy.apply(devices)
    report = {
        "agent_version": __version__,
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": sys.version.split()[0],
        "cuda_driver": {"available": probe["available"], "error": probe["error"],
                        "driver_api_version": probe["driver_version"],
                        "cuda_version": cuda_version_label(probe["driver_version"]),
                        "devices": len(probe["devices"])},
        "devices": [],
        "warnings": warnings,
        "selftests": [],
    }
    for d in devices:
        _, reasons = workloads.device_workloads(d)
        entry = {k: v for k, v in d.to_dict().items() if k not in ("uuid", "pci_bus_id")}
        entry["cannot_run"] = reasons
        report["devices"].append(entry)
    if selftest:
        for d in devices:
            if d.device_type == "gpu" and policy.selects(d):
                if d.display_active and not policy.allow_display_gpu:
                    report["selftests"].append({"slot": d.slot, "ok": False,
                                                "error": "skipped: drives a display (add --allow-display-gpu)"})
                elif not d.cuda:
                    report["selftests"].append({"slot": d.slot, "ok": False,
                                                "error": "skipped: CUDA driver could not open this GPU"})
                else:
                    report["selftests"].append(selftest_gpu(d, driver_factory))
    return report


def render(report: dict) -> str:
    lines = [f"SharedCompute agent {report['agent_version']} on {report['platform']} (Python {report['python']})"]
    cd = report["cuda_driver"]
    if cd["available"]:
        lines.append(f"CUDA driver: API {cd['driver_api_version']} (CUDA {cd['cuda_version']}), {cd['devices']} device(s)")
    else:
        lines.append(f"CUDA driver: not available ({cd['error']})")
    lines.append("")
    for d in report["devices"]:
        if d["device_type"] == "cpu":
            head = f"CPU    {d['model']}"
        else:
            parts = [f"{d['vram_mb']} MB VRAM" + (f" ({d['vram_free_mb']} MB free)" if d["vram_free_mb"] is not None else ""),
                     f"CC {d['compute_capability'] or '?'}", f"driver {d['driver_version'] or '?'}",
                     f"CUDA {'usable' if d['cuda'] else 'not usable'}"]
            if d["display_active"]:
                parts.append("drives a display")
            name = d["model"] if d["model"].lower().startswith(d["vendor"].lower()) else f"{d['vendor']} {d['model']}"
            head = f"GPU {d['slot']}  {name} · " + " · ".join(parts) + f" · via {d['detection']}"
        lines.append(head)
        lines.append(f"       can run: {', '.join(d['workloads']) or 'nothing'}")
        for reason in d["cannot_run"]:
            lines.append(f"       cannot run {reason}")
        lines.append(f"       offered with these flags: {'YES' if d['offered'] else 'no'}"
                     + (f" — {d['offer_note']}" if d["offer_note"] else ""))
    for w in report["warnings"]:
        lines.append(f"WARNING: {w}")
    if report["selftests"]:
        lines.append("")
        for t in report["selftests"]:
            if t["ok"]:
                lines.append(f"Self-test GPU {t['slot']}: PASS · {t['n']}x{t['n']} int32 matmul matches the CPU exactly "
                             f"· kernel {t['kernel_ms']} ms")
            else:
                detail = t.get("error") or f"{t.get('mismatched_elements')} elements differ from the CPU result"
                lines.append(f"Self-test GPU {t['slot']}: FAIL · {detail}")
    return "\n".join(lines)
