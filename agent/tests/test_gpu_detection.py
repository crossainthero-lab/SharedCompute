"""Device discovery. The macOS fixture was recorded on real hardware (an Intel MacBook Pro); the nvidia-smi
fixtures are synthetic samples in the documented CSV format, since no NVIDIA machine was available."""
import json
import sys
import types
from pathlib import Path

from sharedcompute_agent import gpu

FIXTURES = Path(__file__).parent / "fixtures"
FULL = gpu.SMI_QUERIES[0]


def test_parse_nvidia_smi_inventory_full_query():
    gpus = gpu.parse_smi_inventory((FIXTURES / "nvidia_smi_inventory.synthetic.csv").read_text(), FULL)
    first, second = gpus
    assert (first.model, first.vram_mb, first.vram_free_mb, first.compute_capability, first.driver_version) == \
        ("NVIDIA GeForce GTX 1050 Ti", 4096, 3920, "6.1", "535.183.01")
    assert first.display_active is False and second.display_active is True
    assert first.pci_bus_id == "00000000:01:00.0"
    assert len(first.fingerprint) == 32 and first.fingerprint not in first.uuid  # UUID is hashed for reports
    assert "uuid" not in first.to_report() and "pci_bus_id" not in first.to_report()
    assert first.cuda is False  # nvidia-smi alone does not prove CUDA works; the driver probe decides


def test_parse_minimal_query_for_old_drivers():
    (g,) = gpu.parse_smi_inventory("0, GPU-abc, Tesla K20m, 4743, 418.226.00\n", gpu.SMI_QUERIES[2])
    assert g.model == "Tesla K20m" and g.compute_capability is None and g.display_active is None
    assert g.vram_free_mb is None


def test_smi_falls_back_through_queries(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd[1])
        return None if "compute_cap" in cmd[1] else "0, GPU-x, GeForce GTX 750 Ti, 2048, 1900, 470.256.02, 0000:01:00.0, [N/A]\n"

    monkeypatch.setattr(gpu.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu, "_run", fake_run)
    (g,) = gpu.detect_smi()
    assert len(calls) == 2 and g.model == "GeForce GTX 750 Ti" and g.display_active is None


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


def probe(devices, version=11040):
    return {"available": True, "error": None, "driver_version": version, "devices": devices}


def test_merge_cuda_matches_by_pci_bus_not_index():
    gpus = gpu.parse_smi_inventory((FIXTURES / "nvidia_smi_inventory.synthetic.csv").read_text(), FULL)
    # CUDA enumerates the other way round ("fastest first"); PCI ids still line up.
    merged = gpu.merge_cuda(gpus, probe([
        {"ordinal": 0, "name": "GTX 960", "compute_capability": "5.2", "total_mb": 2048, "pci_bus_id": "0000:02:00.0"},
        {"ordinal": 1, "name": "GTX 1050 Ti", "compute_capability": "6.1", "total_mb": 4096, "pci_bus_id": "0000:01:00.0"},
    ]))
    assert [(g.slot, g.cuda_ordinal, g.cuda, g.cuda_driver_version) for g in merged] == [(0, 1, True, 11040), (1, 0, True, 11040)]


def test_merge_cuda_leaves_unmatched_gpus_unusable():
    gpus = gpu.parse_smi_inventory((FIXTURES / "nvidia_smi_inventory.synthetic.csv").read_text(), FULL)
    merged = gpu.merge_cuda(gpus, {"available": False, "error": "libcuda not found", "driver_version": None, "devices": []})
    assert all(not g.cuda and g.cuda_ordinal is None for g in merged)


def test_merge_cuda_without_inventory_reports_driver_devices():
    (g,) = gpu.merge_cuda([], probe([{"ordinal": 0, "name": "GeForce GT 710", "compute_capability": "3.5",
                                      "total_mb": 2001, "pci_bus_id": "0000:05:00.0"}], 9020))
    assert (g.detection, g.cuda, g.compute_capability, g.cuda_driver_version) == ("cuda", True, "3.5", 9020)


def test_merge_cuda_fills_compute_capability_for_old_drivers():
    (g,) = gpu.parse_smi_inventory("0, GPU-abc, Tesla K20m, 4743, 418.226.00\n", gpu.SMI_QUERIES[2])
    (merged,) = gpu.merge_cuda([g], probe([{"ordinal": 0, "name": "Tesla K20m", "compute_capability": "3.5",
                                           "total_mb": 4743, "pci_bus_id": "0000:03:00.0"}], 10010))
    assert merged.compute_capability == "3.5" and merged.cuda_ordinal == 0  # matched by order (no pci id)


def test_pci_key_normalises_domains():
    assert gpu.pci_key("00000000:01:00.0") == gpu.pci_key("0000:01:00.0") == gpu.pci_key("01:00.0")
    assert gpu.pci_key("garbage") is None


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
    def nvmlDeviceGetName(self, h): return b"NVIDIA GeForce GTX 1060 6GB"
    def nvmlDeviceGetUUID(self, h): return "GPU-11111111-2222-3333-4444-555555555555"
    def nvmlDeviceGetMemoryInfo(self, h): return types.SimpleNamespace(total=6144 * 1024 * 1024, used=300 * 1024 * 1024, free=5800 * 1024 * 1024)
    def nvmlDeviceGetCudaComputeCapability(self, h): return (6, 1)
    def nvmlDeviceGetDisplayActive(self, h): return 1
    def nvmlDeviceGetPciInfo(self, h): return types.SimpleNamespace(busId=b"00000000:01:00.0")
    def nvmlDeviceGetUtilizationRates(self, h): return types.SimpleNamespace(gpu=91)
    def nvmlDeviceGetTemperature(self, h, sensor): return 63


def test_nvml_detection_and_metrics(monkeypatch):
    fake = FakeNvml()
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    (g,) = gpu.detect_nvml()
    assert (g.model, g.vram_mb, g.vram_free_mb, g.compute_capability, g.display_active, g.pci_bus_id) == \
        ("NVIDIA GeForce GTX 1060 6GB", 6144, 5800, "6.1", True, "00000000:01:00.0")
    assert gpu.metrics_nvml() == [{"slot": 0, "util_percent": 91.0, "mem_used_mb": 300, "temperature_c": 63.0}]
    assert fake.calls.count("init") == fake.calls.count("shutdown") == 2


def test_nvml_tolerates_missing_newer_functions(monkeypatch):
    fake = FakeNvml()
    del FakeNvml.nvmlDeviceGetDisplayActive  # very old NVML
    try:
        monkeypatch.setitem(sys.modules, "pynvml", fake)
        (g,) = gpu.detect_nvml()
        assert g.display_active is None and g.model.endswith("1060 6GB")
    finally:
        FakeNvml.nvmlDeviceGetDisplayActive = lambda self, h: 1


def test_nvml_init_failure_falls_through(monkeypatch):
    fake = FakeNvml()
    fake.nvmlInit = lambda: (_ for _ in ()).throw(RuntimeError("NVML Shared Library Not Found"))
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    assert gpu.detect_nvml() is None


def test_detect_order_and_cuda_merge(monkeypatch):
    smi = gpu.parse_smi_inventory("0, GPU-x, GeForce GTX 750 Ti, 2048, 1900, 470.256.02, 0000:01:00.0, Disabled\n",
                                  gpu.SMI_QUERIES[1])
    monkeypatch.setattr(gpu, "detect_nvml", lambda: None)
    monkeypatch.setattr(gpu, "detect_smi", lambda: smi)
    monkeypatch.setattr(gpu, "detect_macos", lambda: (_ for _ in ()).throw(AssertionError("not reached")))
    monkeypatch.setattr(gpu.platform, "system", lambda: "Linux")
    (g,) = gpu.detect(lambda: probe([{"ordinal": 0, "name": "GTX 750 Ti", "compute_capability": "5.0",
                                       "total_mb": 2048, "pci_bus_id": "0000:01:00.0"}], 11040))
    assert g.cuda and g.compute_capability == "5.0"


def test_detect_nothing_is_graceful(monkeypatch):
    for name in ("detect_nvml", "detect_smi", "detect_macos"):
        monkeypatch.setattr(gpu, name, lambda: None)
    assert gpu.detect(lambda: {"available": False, "error": "x", "driver_version": None, "devices": []}) == []
    monkeypatch.setattr(gpu, "metrics_nvml", lambda: None)
    monkeypatch.setattr(gpu, "metrics_smi", lambda: None)
    assert gpu.metrics() == []


def test_cpu_device_is_reported_as_cpu():
    cpu = gpu.cpu_device()
    assert cpu.device_type == "cpu" and cpu.vram_mb == 0 and cpu.detection == "os" and "threads" in cpu.model
