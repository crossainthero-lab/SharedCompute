import pytest

from fake_cuda import FakeCuda
from sharedcompute_agent import cuda, workloads
from sharedcompute_agent.errors import Incompatible
from sharedcompute_agent.gpu import GpuInfo
from sharedcompute_agent.policy import OfferPolicy


def nvidia(slot=0, cc="6.1", display=False, cuda_ok=True, vram=4096, driver=11040, uuid=None):
    return GpuInfo(slot=slot, vendor="NVIDIA", model=f"GeForce Test {slot}", vram_mb=vram, detection="nvidia-smi",
                   fingerprint=f"{slot:02d}" * 16, cuda=cuda_ok, compute_capability=cc, cuda_driver_version=driver,
                   display_active=display, uuid=uuid or f"GPU-{slot}aaa-bbbb", cuda_ordinal=slot if cuda_ok else None)


def cpu():
    return GpuInfo(slot=0, device_type="cpu", vendor="Intel", model="Test CPU (8 threads)", vram_mb=0, detection="os")


# --- Opt-in policy ---------------------------------------------------------------------------------

def test_nothing_is_offered_by_default():
    devices = [nvidia(0), nvidia(1), cpu()]
    OfferPolicy().apply(devices)
    assert [d.offered for d in devices] == [False, False, False]
    assert "--gpu 0" in devices[0].offer_note and "--offer-cpu" in devices[2].offer_note
    assert devices[0].workloads == ["agent.cuda_matmul"]  # capable, just not offered


def test_explicit_gpu_selection_by_index_and_uuid():
    devices = [nvidia(0), nvidia(1, uuid="GPU-77fe0000-1111"), cpu()]
    warnings = OfferPolicy(gpus=("0", "GPU-77fe"), offer_cpu=True).apply(devices)
    assert [d.offered for d in devices] == [True, True, True] and warnings == []


def test_display_gpu_is_refused_unless_explicitly_allowed():
    devices = [nvidia(0, display=True)]
    warnings = OfferPolicy(gpus=("0",)).apply(devices)
    assert devices[0].offered is False and "display" in devices[0].offer_note and warnings
    OfferPolicy(gpus=("0",), allow_display_gpu=True).apply(devices)
    assert devices[0].offered is True


@pytest.mark.parametrize("kwargs,reason", [
    ({"cc": "2.1"}, "below 3.0"),                     # Fermi
    ({"driver": 5000}, "older than CUDA 5.5"),
    ({"cuda_ok": False}, "could not open"),
    ({"vram": 32}, "below 64 MB"),
])
def test_selected_but_incompatible_gpu_is_not_offered(kwargs, reason):
    devices = [nvidia(0, **kwargs)]
    warnings = OfferPolicy(gpus=("0",)).apply(devices)
    assert devices[0].offered is False and devices[0].workloads == []
    assert reason in devices[0].offer_note and warnings


def test_non_nvidia_gpu_cannot_run_cuda_work():
    amd = GpuInfo(slot=1, vendor="AMD", model="Radeon Pro 5300M", vram_mb=4096, detection="system_profiler")
    OfferPolicy(gpus=("1",)).apply([amd])
    assert amd.offered is False and "not an NVIDIA GPU" in amd.offer_note


def test_unknown_selector_warns():
    assert OfferPolicy(gpus=("3",)).apply([nvidia(0)]) == ["--gpu 3 does not match any detected GPU"]


# --- Workloads ---------------------------------------------------------------------------------------

PARAMS = {"seed": 99, "n": 16, "repeats": 2, "samples": 3, "nonce": "ab" * 16}


def options(**kw):
    return workloads.RunOptions(driver_factory=lambda: cuda.Driver(lib=FakeCuda(**kw)))


def test_gpu_path_reports_gpu_and_commits_to_result():
    out = workloads.cuda_matmul(PARAMS, lambda: False, nvidia(0), options())
    assert out["device"]["type"] == "gpu" and out["device"]["backend"] == "cuda-driver-ptx"
    assert out["device"]["fingerprint"] == nvidia(0).fingerprint and out["repeats"] == 2
    a, b = workloads.seeded_matrices(99, 16)
    c = workloads.cpu_matmul(a, b, 16)
    assert out["checksum"] == sum(c)
    rows = workloads.challenge_rows(out["merkle_root"], PARAMS["nonce"], 3, 16)
    assert [s["row"] for s in out["samples"]] == rows
    for s in out["samples"]:
        assert s["values"] == c[s["row"] * 16: (s["row"] + 1) * 16]


def test_no_cuda_without_fallback_is_a_graceful_rejection():
    with pytest.raises(Incompatible):
        workloads.cuda_matmul(PARAMS, lambda: False, nvidia(0, cuda_ok=False), options())


def test_cpu_fallback_is_explicit_and_labelled():
    opts = options()
    opts.allow_cpu_fallback = True
    out = workloads.cuda_matmul(PARAMS, lambda: False, nvidia(0, cuda_ok=False), opts)
    assert out["device"] == {"type": "cpu", "backend": "python-cpu-fallback",
                             "fallback_reason": "no CUDA-usable GPU for this job"}
    assert opts.log and "CPU fallback" in opts.log[0]


def test_cuda_error_falls_back_only_when_allowed():
    with pytest.raises(cuda.CudaError):
        workloads.cuda_matmul(PARAMS, lambda: False, nvidia(0), options(fail={"cuLaunchKernel": 999}))
    opts = options(fail={"cuLaunchKernel": 999})
    opts.allow_cpu_fallback = True
    out = workloads.cuda_matmul(PARAMS, lambda: False, nvidia(0), opts)
    assert out["device"]["type"] == "cpu" and "CUDA_ERROR_UNKNOWN" in out["device"]["fallback_reason"]


def test_merkle_proofs_roundtrip_for_odd_sizes():
    for n in (1, 3, 5, 16):
        c = list(range(n * n))
        levels = workloads.merkle_tree([workloads.merkle_leaf(i, c[i * n:(i + 1) * n]) for i in range(n)])
        root = levels[-1][0]
        for i in range(n):
            node = workloads.merkle_leaf(i, c[i * n:(i + 1) * n])
            idx = i
            for sib in workloads.merkle_proof(levels, i):
                node = workloads.merkle_parent(node, bytes.fromhex(sib)) if idx % 2 == 0 else \
                    workloads.merkle_parent(bytes.fromhex(sib), node)
                idx //= 2
            assert node == root


def test_hashchain_reports_cpu():
    out = workloads.hashchain({"seed": "00" * 32, "iterations": 10}, lambda: False)
    assert out["device"]["type"] == "cpu"
