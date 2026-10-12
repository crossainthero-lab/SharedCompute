"""GPU discovery parsers. The macOS fixture was recorded on real hardware; the nvidia-smi fixtures
are synthetic samples in the documented CSV format (no NVIDIA machine was available)."""
import json
import sys
import types
from pathlib import Path

from sharedcompute_agent import gpu

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_nvidia_smi_inventory():
    gpus = gpu.parse_smi_inventory((FIXTURES / "nvidia_smi_inventory.synthetic.csv").read_text(), with_cc=True)
    assert [g.model for g in gpus] == ["NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 3090"]
    first = gpus[0]
    assert (first.slot, first.vram_mb, first.cuda, first.compute_capability, first.driver_version) == \
        (0, 24564, True, "8.9", "550.54.15")
    assert first.detection == "nvidia-smi"
    assert len(first.fingerprint) == 32 and "GPU-" not in first.fingerprint  # UUID is hashed


def test_parse_nvidia_smi_without_compute_cap():
    out = "0, GPU-abc, Tesla T4, 15360, 535.104.05\n"
    (g,) = gpu.parse_smi_inventory(out, with_cc=False)
    assert g.model == "Tesla T4" and g.compute_capability is None


def test_parse_nvidia_smi_metrics_handles_not_available():
    metrics = gpu.parse_smi_metrics((FIXTURES / "nvidia_smi_metrics.synthetic.csv").read_text())
    assert metrics[0] == {"slot": 0, "util_percent": 37.0, "mem_used_mb": 1024, "temperature_c": 45.0}
    assert metrics[1] == {"slot": 1, "util_percent": None, "mem_used_mb": 512, "temperature_c": None}


def test_parse_system_profiler_recorded_on_intel_mac():
    data = json.loads((FIXTURES / "system_profiler_intel_amd.json").read_text())
    intel, amd = gpu.parse_system_profiler(data)
    assert (intel.vendor, intel.model, intel.vram_mb, intel.integrated, intel.cuda) == \
        ("Intel", "Intel UHD Graphics 630", 1536, True, False)
    assert (amd.vendor, amd.model, amd.vram_mb, amd.integrated, amd.cuda) == \
        ("AMD", "AMD Radeon Pro 5300M", 4096, False, False)


def test_parse_system_profiler_apple_silicon_shape():
    data = {"SPDisplaysDataType": [{"sppci_model": "Apple M2 Pro", "sppci_device_type": "spdisplays_gpu",
                                    "spdisplays_vendor": "sppci_vendor_Apple", "sppci_bus": "spdisplays_builtin"}]}
    (g,) = gpu.parse_system_profiler(data)
    assert (g.vendor, g.vram_mb, g.integrated) == ("Apple", 0, True)


class FakeNvml(types.ModuleType):
    NVML_TEMPERATURE_GPU = 0

    def __init__(self):
        super().__init__("pynvml")
        self.calls = []

    def nvmlInit(self): self.calls.append("init")
    def nvmlShutdown(self): self.calls.append("shutdown")
    def nvmlSystemGetDriverVersion(self): return b"550.54.15"
    def nvmlDeviceGetCount(self): return 1
    def nvmlDeviceGetHandleByIndex(self, i): return f"h{i}"
    def nvmlDeviceGetName(self, h): return b"NVIDIA A100-SXM4-80GB"
    def nvmlDeviceGetUUID(self, h): return "GPU-11111111-2222-3333-4444-555555555555"
    def nvmlDeviceGetMemoryInfo(self, h): return types.SimpleNamespace(total=81920 * 1024 * 1024, used=2048 * 1024 * 1024)
    def nvmlDeviceGetCudaComputeCapability(self, h): return (8, 0)
    def nvmlDeviceGetUtilizationRates(self, h): return types.SimpleNamespace(gpu=91)
    def nvmlDeviceGetTemperature(self, h, sensor): return 63


def test_nvml_detection_and_metrics(monkeypatch):
    fake = FakeNvml()
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    (g,) = gpu.detect_nvml()
    assert (g.model, g.vram_mb, g.compute_capability, g.driver_version, g.detection) == \
        ("NVIDIA A100-SXM4-80GB", 81920, "8.0", "550.54.15", "nvml")
    assert gpu.metrics_nvml() == [{"slot": 0, "util_percent": 91.0, "mem_used_mb": 2048, "temperature_c": 63.0}]
    assert fake.calls.count("init") == fake.calls.count("shutdown") == 2


def test_nvml_init_failure_falls_through(monkeypatch):
    fake = FakeNvml()
    fake.nvmlInit = lambda: (_ for _ in ()).throw(RuntimeError("NVML Shared Library Not Found"))
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    assert gpu.detect_nvml() is None


def test_detect_order_and_graceful_empty_fallback(monkeypatch):
    sentinel = [gpu.GpuInfo(slot=0, vendor="NVIDIA", model="X", vram_mb=1, detection="nvidia-smi")]
    monkeypatch.setattr(gpu, "detect_nvml", lambda: None)
    monkeypatch.setattr(gpu, "detect_smi", lambda: sentinel)
    monkeypatch.setattr(gpu, "detect_macos", lambda: (_ for _ in ()).throw(AssertionError("not reached")))
    assert gpu.detect() is sentinel
    monkeypatch.setattr(gpu, "detect_smi", lambda: None)
    monkeypatch.setattr(gpu, "detect_macos", lambda: None)
    assert gpu.detect() == []
    monkeypatch.setattr(gpu, "metrics_nvml", lambda: None)
    monkeypatch.setattr(gpu, "metrics_smi", lambda: None)
    assert gpu.metrics() == []
