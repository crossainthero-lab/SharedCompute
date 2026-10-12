"""GPU discovery and live metrics.

Detection order, first success wins:

1. NVML through the optional ``nvidia-ml-py`` package (``import pynvml``)
2. ``nvidia-smi`` CSV queries
3. macOS ``system_profiler`` (reports Intel/AMD/Apple GPUs; never CUDA-capable)

Hardware UUIDs are hashed before leaving the machine. Everything returned here is what the
operating system or driver reports; none of it is cryptographically attested.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass

SMI_TIMEOUT = 10


@dataclass
class GpuInfo:
    slot: int
    vendor: str
    model: str
    vram_mb: int
    detection: str
    fingerprint: str | None = None
    cuda: bool = False
    compute_capability: str | None = None
    driver_version: str | None = None
    integrated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def fingerprint(uuid: str) -> str:
    return hashlib.sha256(uuid.strip().encode()).hexdigest()[:32]


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


# --- NVML ---------------------------------------------------------------------------------

def _nvml():
    try:
        import pynvml  # type: ignore[import-not-found]
    except ImportError:
        return None
    return pynvml


def detect_nvml() -> list[GpuInfo] | None:
    nv = _nvml()
    if nv is None:
        return None
    try:
        nv.nvmlInit()
    except Exception:
        return None
    try:
        driver = _text(nv.nvmlSystemGetDriverVersion())
        gpus = []
        for i in range(nv.nvmlDeviceGetCount()):
            h = nv.nvmlDeviceGetHandleByIndex(i)
            try:
                major, minor = nv.nvmlDeviceGetCudaComputeCapability(h)
                cc = f"{major}.{minor}"
            except Exception:
                cc = None
            gpus.append(GpuInfo(
                slot=i, vendor="NVIDIA", model=_text(nv.nvmlDeviceGetName(h)),
                vram_mb=int(nv.nvmlDeviceGetMemoryInfo(h).total // (1024 * 1024)), detection="nvml",
                fingerprint=fingerprint(_text(nv.nvmlDeviceGetUUID(h))), cuda=True,
                compute_capability=cc, driver_version=driver,
            ))
        return gpus
    except Exception:
        return None
    finally:
        try:
            nv.nvmlShutdown()
        except Exception:
            pass


def metrics_nvml() -> list[dict] | None:
    nv = _nvml()
    if nv is None:
        return None
    try:
        nv.nvmlInit()
    except Exception:
        return None
    try:
        out = []
        for i in range(nv.nvmlDeviceGetCount()):
            h = nv.nvmlDeviceGetHandleByIndex(i)
            out.append({
                "slot": i,
                "util_percent": float(nv.nvmlDeviceGetUtilizationRates(h).gpu),
                "mem_used_mb": int(nv.nvmlDeviceGetMemoryInfo(h).used // (1024 * 1024)),
                "temperature_c": float(nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)),
            })
        return out
    except Exception:
        return None
    finally:
        try:
            nv.nvmlShutdown()
        except Exception:
            pass


# --- nvidia-smi ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SMI_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _csv_rows(output: str) -> list[list[str]]:
    return [[c.strip() for c in line.split(",")] for line in output.strip().splitlines() if line.strip()]


def parse_smi_inventory(output: str, with_cc: bool) -> list[GpuInfo]:
    gpus = []
    for row in _csv_rows(output):
        if len(row) < (6 if with_cc else 5):
            continue
        index, uuid, name, mem, driver = row[:5]
        cc = row[5] if with_cc and re.fullmatch(r"\d+\.\d+", row[5]) else None
        gpus.append(GpuInfo(slot=int(index), vendor="NVIDIA", model=name, vram_mb=int(float(mem)),
                            detection="nvidia-smi", fingerprint=fingerprint(uuid), cuda=True,
                            compute_capability=cc, driver_version=driver))
    return gpus


def detect_smi() -> list[GpuInfo] | None:
    if not shutil.which("nvidia-smi"):
        return None
    base = "index,uuid,name,memory.total,driver_version"
    out = _run(["nvidia-smi", f"--query-gpu={base},compute_cap", "--format=csv,noheader,nounits"])
    if out is not None:
        return parse_smi_inventory(out, with_cc=True)
    # Older drivers do not know the compute_cap field.
    out = _run(["nvidia-smi", f"--query-gpu={base}", "--format=csv,noheader,nounits"])
    return parse_smi_inventory(out, with_cc=False) if out is not None else None


def _num(text: str, cast=float):
    try:
        return cast(float(text))
    except ValueError:
        return None  # "[N/A]" and similar


def parse_smi_metrics(output: str) -> list[dict]:
    return [{"slot": int(r[0]), "util_percent": _num(r[1]), "mem_used_mb": _num(r[2], int),
             "temperature_c": _num(r[3])} for r in _csv_rows(output) if len(r) >= 4]


def metrics_smi() -> list[dict] | None:
    if not shutil.which("nvidia-smi"):
        return None
    out = _run(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,temperature.gpu",
                "--format=csv,noheader,nounits"])
    return parse_smi_metrics(out) if out is not None else None


# --- macOS --------------------------------------------------------------------------------

def _vram_mb(text: str | None) -> int:
    m = re.match(r"\s*([\d.]+)\s*(MB|GB)", text or "", re.I)
    if not m:
        return 0
    value = float(m.group(1))
    return int(value * 1024 if m.group(2).upper() == "GB" else value)


def _mac_vendor(raw: str, model: str) -> str:
    raw = raw.lower()
    for key, name in (("amd", "AMD"), ("intel", "Intel"), ("nvidia", "NVIDIA"), ("apple", "Apple")):
        if key in raw or key in model.lower():
            return name
    return "Unknown"


def parse_system_profiler(data: dict) -> list[GpuInfo]:
    gpus = []
    for i, g in enumerate(d for d in data.get("SPDisplaysDataType", []) if d.get("sppci_device_type", "").endswith("gpu")):
        model = g.get("sppci_model") or g.get("_name", "Unknown GPU")
        vram = g.get("spdisplays_vram") or g.get("_spdisplays_vram") or g.get("spdisplays_vram_shared")
        gpus.append(GpuInfo(slot=i, vendor=_mac_vendor(g.get("spdisplays_vendor", ""), model), model=model,
                            vram_mb=_vram_mb(vram), detection="system_profiler",
                            integrated=g.get("sppci_bus") == "spdisplays_builtin"))
    return gpus


def detect_macos() -> list[GpuInfo] | None:
    if platform.system() != "Darwin":
        return None
    out = _run(["system_profiler", "-json", "SPDisplaysDataType"])
    if out is None:
        return None
    try:
        return parse_system_profiler(json.loads(out))
    except (ValueError, TypeError):
        return None


# --- Entry points -------------------------------------------------------------------------

def detect() -> list[GpuInfo]:
    for detector in (detect_nvml, detect_smi, detect_macos):
        found = detector()
        if found:
            return found
    return []


def metrics() -> list[dict]:
    """Utilisation, memory and temperature. Only available for NVIDIA devices."""
    return metrics_nvml() or metrics_smi() or []
