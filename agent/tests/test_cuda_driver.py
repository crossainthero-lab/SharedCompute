"""The ctypes CUDA binding against an in-process fake libcuda (see fake_cuda.py for what that does and does
not cover). The PTX kernel itself is only exercised on a real NVIDIA GPU."""
import sys

import pytest

from fake_cuda import FakeCuda
from sharedcompute_agent import cuda, workloads
from sharedcompute_agent.errors import Cancelled, Incompatible


def driver(**kw):
    fake = FakeCuda(**kw)
    return cuda.Driver(lib=fake), fake


def test_probe_reports_driver_and_devices():
    result = cuda.probe(lambda: cuda.Driver(lib=FakeCuda(driver_version=11040)))
    assert result["available"] and result["driver_version"] == 11040
    assert result["devices"] == [{"ordinal": 0, "name": "Fake GeForce GTX 1050 Ti", "compute_capability": "6.1",
                                  "total_mb": 4096, "pci_bus_id": "0000:01:00.0"}]


def test_probe_never_raises():
    result = cuda.probe(lambda: cuda.Driver(lib=FakeCuda(fail={"cuInit": 100})))
    assert result["available"] is False and "CUDA_ERROR_NO_DEVICE" in result["error"]

    def missing():
        raise cuda.CudaUnavailable("libcuda not found")
    assert cuda.probe(missing)["error"] == "libcuda not found"


def test_no_cuda_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(cuda.CudaUnavailable):
        cuda.load_library()


@pytest.mark.parametrize("n", [1, 15, 16, 33])
def test_matmul_marshalling_and_result(n):
    drv, fake = driver()
    a, b = workloads.seeded_matrices(7, n)
    c, kernel_ms = cuda.run_matmul_i32(drv, 0, a, b, n, 3, lambda: False)
    assert c == workloads.cpu_matmul(a, b, n)
    assert kernel_ms > 0 and fake.launches == 4  # one warm-up + three timed
    assert fake.memory == {} and fake.contexts == 0 and fake.modules == 0  # everything released


def test_legacy_driver_entry_points_are_used_when_v2_missing():
    drv, fake = driver(legacy_only=True)
    a, b = workloads.seeded_matrices(1, 8)
    c, _ = cuda.run_matmul_i32(drv, 0, a, b, 8, 1, lambda: False)
    assert fake.cuMemAlloc_v2 is None and fake.cuCtxCreate_v2 is None  # only legacy names exist
    assert c == workloads.cpu_matmul(a, b, 8)


def test_insufficient_free_vram_is_a_graceful_incompatibility():
    drv, fake = driver(free_mb=10)
    with pytest.raises(Incompatible, match="VRAM free"):
        cuda.run_matmul_i32(drv, 0, [0] * 4, [0] * 4, 2, 1, lambda: False)
    assert fake.contexts == 0


def test_jit_failure_includes_the_driver_log():
    drv, fake = driver(jit_error="ptxas fatal: Unsupported .version 3.2")
    with pytest.raises(cuda.CudaError) as err:
        cuda.run_matmul_i32(drv, 0, [0] * 4, [0] * 4, 2, 1, lambda: False)
    assert err.value.name == "CUDA_ERROR_INVALID_PTX" and "Unsupported .version" in str(err.value)
    assert fake.contexts == 0


def test_allocation_failure_releases_earlier_buffers():
    drv, fake = driver()
    calls = {"n": 0}
    real = fake.cuMemAlloc_v2

    def flaky(out, size):
        calls["n"] += 1
        return 2 if calls["n"] == 2 else real(out, size)
    fake.cuMemAlloc_v2 = flaky
    with pytest.raises(cuda.CudaError, match="OUT_OF_MEMORY"):
        cuda.run_matmul_i32(drv, 0, [1] * 16, [1] * 16, 4, 1, lambda: False)
    assert fake.memory == {} and fake.contexts == 0


def test_cancellation_between_launches_cleans_up():
    drv, fake = driver()
    with pytest.raises(Cancelled):
        cuda.run_matmul_i32(drv, 0, [1] * 16, [1] * 16, 4, 5, lambda: True)
    assert fake.memory == {} and fake.contexts == 0


def test_launch_error_is_named():
    drv, _ = driver(fail={"cuLaunchKernel": 999})
    with pytest.raises(cuda.CudaError, match="CUDA_ERROR_UNKNOWN"):
        cuda.run_matmul_i32(drv, 0, [1] * 16, [1] * 16, 4, 1, lambda: False)
