"""Device discovery and live metrics.

GPU inventory, first success wins:

1. NVML through the optional ``nvidia-ml-py`` package (``import pynvml``)
2. ``nvidia-smi`` CSV queries (ships with every NVIDIA driver)
3. macOS ``system_profiler`` (reports Intel/AMD/Apple GPUs; never CUDA-capable)

NVIDIA GPUs are then cross-checked with the CUDA driver itself (``cuda.probe``), matched by PCI bus id:
only a GPU the driver can actually open is marked CUDA-usable, and the driver supplies the compute
capability and CUDA driver API version. The CPU is reported as its own device so CPU-only machines can
take part in development.

Hardware UUIDs are hashed before leaving the machine. Everything here is what the OS or driver reports;
none of it is cryptographically attested.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

from . import cuda

SMI_TIMEOUT = 10
REPORT_FIELDS = ("slot", "device_type", "vendor", "model", "vram_mb", "vram_free_mb", "detection", "fingerprint",
                 "cuda", "compute_capability", "cuda_driver_version", "driver_version", "integrated",
                 "display_active", "offered", "offer_note", "workloads")


@dataclass
class GpuInfo:
    """A compute device. Despite the name, `device_type` may be 'cpu'."""
    slot: int
    vendor: str
    model: str
    vram_mb: int
    detection: str
    device_type: str = "gpu"
    fingerprint: Optional[str] = None
    cuda: bool = False                      # CUDA driver opened this GPU successfully
    compute_capability: Optional[str] = None
    cuda_driver_version: Optional[int] = None
    driver_version: Optional[str] = None
    integrated: bool = False
    display_active: Optional[bool] = None   # drives a monitor: never used unless explicitly allowed
    vram_free_mb: Optional[int] = None
    # Local only, never sent: used to match NVML/nvidia-smi entries to CUDA ordinals.
    uuid: Optional[str] = None
    pci_bus_id: Optional[str] = None
    cuda_ordinal: Optional[int] = None
    # Set by the offer policy.
    offered: bool = False
    offer_note: Optional[str] = None
    workloads: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_report(self) -> dict:
        data = asdict(self)
        return {k: data[k] for k in REPORT_FIELDS}


def fingerprint(uuid: str) -> str:
    return hashlib.sha256(uuid.strip().encode()).hexdigest()[:32]


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def pci_key(bus_id: Optional[str]):
    """Normalise '00000000:01:00.0' and '0000:01:00.0' to comparable integers."""
    if not bus_id:
        return None
    m = re.match(r"^(?:([0-9a-fA-F]+):)?([0-9a-fA-F]+):([0-9a-fA-F]+)\.([0-9a-fA-F]+)$", bus_id.strip())
    if not m:
        return None
    return tuple(int(part or "0", 16) for part in m.groups())


# --- NVML ---------------------------------------------------------------------------------

def _nvml():
    try:
        import pynvml  # type: ignore[import-not-found]
    except ImportError:
        return None
    return pynvml


def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def detect_nvml() -> Optional[List[GpuInfo]]:
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
            cc = _try(lambda: "{}.{}".format(*nv.nvmlDeviceGetCudaComputeCapability(h)))
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            uuid = _text(nv.nvmlDeviceGetUUID(h))
            gpus.append(GpuInfo(
                slot=i, vendor="NVIDIA", model=_text(nv.nvmlDeviceGetName(h)), vram_mb=int(mem.total // (1024 * 1024)),
                vram_free_mb=int(mem.free // (1024 * 1024)), detection="nvml", fingerprint=fingerprint(uuid),
                compute_capability=cc, driver_version=driver, uuid=uuid,
                display_active=_try(lambda: bool(nv.nvmlDeviceGetDisplayActive(h))),
                pci_bus_id=_try(lambda: _text(nv.nvmlDeviceGetPciInfo(h).busId)),
            ))
        return gpus
    except Exception:
        return None
    finally:
        _try(nv.nvmlShutdown)


def metrics_nvml() -> Optional[List[dict]]:
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
        _try(nv.nvmlShutdown)


# --- nvidia-smi ---------------------------------------------------------------------------

# Tried in order; older drivers reject newer field names (compute_cap needs ~510+).
SMI_QUERIES = [
    ["index", "uuid", "name", "memory.total", "memory.free", "driver_version", "pci.bus_id", "display_active",
     "compute_cap"],
    ["index", "uuid", "name", "memory.total", "memory.free", "driver_version", "pci.bus_id", "display_active"],
    ["index", "uuid", "name", "memory.total", "driver_version"],
]


def _run(cmd: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              timeout=SMI_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _csv_rows(output: str) -> List[List[str]]:
    return [[c.strip() for c in line.split(",")] for line in output.strip().splitlines() if line.strip()]


def _num(text: str, cast=float):
    try:
        return cast(float(text))
    except (TypeError, ValueError):
        return None  # "[N/A]", "[Not Supported]" and similar


def parse_smi_inventory(output: str, fields: List[str]) -> List[GpuInfo]:
    gpus = []
    for row in _csv_rows(output):
        if len(row) < len(fields):
            continue
        r = dict(zip(fields, row))
        cc = r.get("compute_cap")
        display = r.get("display_active")
        gpus.append(GpuInfo(
            slot=int(r["index"]), vendor="NVIDIA", model=r["name"], vram_mb=int(float(r["memory.total"])),
            vram_free_mb=_num(r.get("memory.free"), int), detection="nvidia-smi", fingerprint=fingerprint(r["uuid"]),
            compute_capability=cc if cc and re.fullmatch(r"\d+\.\d+", cc) else None,
            driver_version=r["driver_version"], uuid=r["uuid"], pci_bus_id=r.get("pci.bus_id"),
            display_active=None if display in (None, "") or display.startswith("[") else display.lower() == "enabled",
        ))
    return gpus


def detect_smi() -> Optional[List[GpuInfo]]:
    if not shutil.which("nvidia-smi"):
        return None
    for fields in SMI_QUERIES:
        out = _run(["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"])
        if out is not None:
            return parse_smi_inventory(out, fields)
    return None


def parse_smi_metrics(output: str) -> List[dict]:
    return [{"slot": int(r[0]), "util_percent": _num(r[1]), "mem_used_mb": _num(r[2], int),
             "temperature_c": _num(r[3])} for r in _csv_rows(output) if len(r) >= 4]


def metrics_smi() -> Optional[List[dict]]:
    if not shutil.which("nvidia-smi"):
        return None
    out = _run(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,temperature.gpu",
                "--format=csv,noheader,nounits"])
    return parse_smi_metrics(out) if out is not None else None


# --- macOS --------------------------------------------------------------------------------

def _vram_mb(text: Optional[str]) -> int:
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


def parse_system_profiler(data: dict) -> List[GpuInfo]:
    gpus = []
    entries = [d for d in data.get("SPDisplaysDataType", []) if d.get("sppci_device_type", "").endswith("gpu")]
    for i, g in enumerate(entries):
        model = g.get("sppci_model") or g.get("_name", "Unknown GPU")
        vram = g.get("spdisplays_vram") or g.get("_spdisplays_vram") or g.get("spdisplays_vram_shared")
        gpus.append(GpuInfo(slot=i, vendor=_mac_vendor(g.get("spdisplays_vendor", ""), model), model=model,
                            vram_mb=_vram_mb(vram), detection="system_profiler",
                            integrated=g.get("sppci_bus") == "spdisplays_builtin"))
    return gpus


def detect_macos() -> Optional[List[GpuInfo]]:
    if platform.system() != "Darwin":
        return None
    out = _run(["system_profiler", "-json", "SPDisplaysDataType"])
    if out is None:
        return None
    try:
        return parse_system_profiler(json.loads(out))
    except (ValueError, TypeError):
        return None


# --- CUDA cross-check ---------------------------------------------------------------------

def merge_cuda(gpus: List[GpuInfo], probe: dict) -> List[GpuInfo]:
    """Mark GPUs the CUDA driver can open, and take compute capability and driver version from it."""
    if not probe.get("available"):
        return gpus
    by_pci = {pci_key(d["pci_bus_id"]): d for d in probe["devices"] if pci_key(d["pci_bus_id"])}
    matched = set()
    for g in gpus:
        if g.vendor != "NVIDIA":
            continue
        d = by_pci.get(pci_key(g.pci_bus_id))
        if d is None and g.pci_bus_id is None and len(probe["devices"]) == len(gpus):
            d = probe["devices"][g.slot]  # old drivers without pci.bus_id: CUDA_DEVICE_ORDER=PCI_BUS_ID ordering
        if d is None:
            continue
        matched.add(d["ordinal"])
        g.cuda = True
        g.cuda_ordinal = d["ordinal"]
        g.cuda_driver_version = probe["driver_version"]
        g.compute_capability = g.compute_capability or d["compute_capability"]
    if not gpus:  # no NVML / nvidia-smi, but the driver works: report straight from CUDA
        for d in probe["devices"]:
            gpus.append(GpuInfo(slot=d["ordinal"], vendor="NVIDIA", model=d["name"], vram_mb=d["total_mb"],
                                detection="cuda", cuda=True, cuda_ordinal=d["ordinal"],
                                compute_capability=d["compute_capability"],
                                cuda_driver_version=probe["driver_version"], pci_bus_id=d["pci_bus_id"]))
    return gpus


# --- CPU ----------------------------------------------------------------------------------

def cpu_model() -> str:
    if platform.system() == "Darwin":
        out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if out and out.strip():
            return out.strip()
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "CPU"


def cpu_device() -> GpuInfo:
    model = cpu_model()
    threads = os.cpu_count() or 1
    vendor = "Intel" if "intel" in model.lower() else "AMD" if "amd" in model.lower() else (
        "Apple" if "apple" in model.lower() else platform.machine() or "CPU")
    return GpuInfo(slot=0, device_type="cpu", vendor=vendor, model=f"{model} ({threads} threads)"[:120],
                   vram_mb=0, detection="os")


# --- Entry points -------------------------------------------------------------------------

def detect(cuda_probe: Callable[[], dict] = cuda.probe) -> List[GpuInfo]:
    inventory: List[GpuInfo] = []
    for detector in (detect_nvml, detect_smi, detect_macos):
        found = detector()
        if found:
            inventory = found
            break
    if platform.system() == "Darwin" and inventory and all(g.vendor != "NVIDIA" for g in inventory):
        return inventory  # no CUDA on macOS; skip the probe
    return merge_cuda(inventory, cuda_probe())


def metrics() -> List[dict]:
    """Utilisation, memory and temperature. Only available for NVIDIA devices."""
    return metrics_nvml() or metrics_smi() or []
