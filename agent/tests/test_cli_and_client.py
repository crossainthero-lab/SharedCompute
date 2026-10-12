import os
import re
import ssl
import stat
from pathlib import Path

import pytest

from fake_cuda import FakeCuda
from sharedcompute_agent import __main__ as cli
from sharedcompute_agent import __version__, cuda
from sharedcompute_agent.client import InsecureServerError, require_secure_url, tls_context
from sharedcompute_agent.diagnose import diagnose, render
from sharedcompute_agent.gpu import GpuInfo
from sharedcompute_agent.policy import OfferPolicy

KERNEL = Path(__file__).resolve().parents[1] / "sharedcompute_agent" / "kernels" / "matmul_i32.ptx"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8765", "http://localhost:8765", "https://market.example.com",
                                 "https://box.tailnet-1234.ts.net"])
def test_secure_urls_accepted(url):
    require_secure_url(url)


@pytest.mark.parametrize("url", ["http://192.168.1.20:8765", "http://market.example.com", "ftp://x", "nonsense"])
def test_insecure_urls_refused(url):
    with pytest.raises(InsecureServerError):
        require_secure_url(url)


def test_tls_context_verifies_and_requires_tls12():
    ctx = tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_run_refuses_plain_http_to_remote_host(monkeypatch, capsys):
    monkeypatch.setenv("SHAREDCOMPUTE_NODE_TOKEN", "scn_test")
    assert cli.main(["run", "--server", "http://10.0.0.5:8765"]) == 2
    assert "Refusing plain HTTP" in capsys.readouterr().err


def test_run_requires_a_token(monkeypatch, capsys):
    monkeypatch.delenv("SHAREDCOMPUTE_NODE_TOKEN", raising=False)
    assert cli.main(["run"]) == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_world_readable_token_file_warns(tmp_path, capsys, monkeypatch):
    token = tmp_path / "node.token"
    token.write_text("scn_x\n")
    token.chmod(0o644)

    class Args:
        token_file = str(token)
    assert cli._token(Args) == "scn_x"
    assert "chmod 600" in capsys.readouterr().err
    token.chmod(stat.S_IRUSR | stat.S_IWUSR)
    cli._token(Args)
    assert capsys.readouterr().err == ""


def test_env_policy(monkeypatch):
    monkeypatch.setenv("SHAREDCOMPUTE_GPUS", "0, 2")
    monkeypatch.setenv("SHAREDCOMPUTE_OFFER_CPU", "1")

    class Args:
        gpu = None
        offer_cpu = False
        allow_display_gpu = False
    p = cli._policy(Args)
    assert p.gpus == ("0", "2") and p.offer_cpu


def gtx(display=False):
    return GpuInfo(slot=0, vendor="NVIDIA", model="NVIDIA GeForce GTX 1050 Ti", vram_mb=4096, vram_free_mb=3900,
                   detection="nvidia-smi", fingerprint="ab" * 16, cuda=True, compute_capability="6.1",
                   cuda_driver_version=12020, display_active=display, cuda_ordinal=0, driver_version="535.183.01")


def cpu():
    return GpuInfo(slot=0, device_type="cpu", vendor="Intel", model="Test CPU (8 threads)", vram_mb=0, detection="os")


def run_diag(policy, fake=None, display=False):
    fake = fake or FakeCuda()
    return diagnose(policy, selftest=True, detector=lambda: [gtx(display)], cpu_detector=cpu,
                    cuda_probe=lambda: cuda.probe(lambda: cuda.Driver(lib=fake)),
                    driver_factory=lambda: cuda.Driver(lib=fake))


def test_diagnose_selftest_passes_on_working_driver():
    report = run_diag(OfferPolicy(gpus=("0",)))
    (t,) = report["selftests"]
    assert t["ok"] and t["mismatched_elements"] == 0
    text = render(report)
    assert "Self-test GPU 0: PASS" in text and "CUDA 12.2" in text and "offered with these flags: YES" in text
    assert "uuid" not in report["devices"][0]


def test_diagnose_detects_wrong_results():
    report = run_diag(OfferPolicy(gpus=("0",)), fake=FakeCuda(corrupt=True))
    assert report["selftests"][0]["ok"] is False and report["selftests"][0]["mismatched_elements"] == 1
    assert "FAIL" in render(report)


def test_diagnose_never_touches_unselected_or_display_gpus():
    fake = FakeCuda()
    assert run_diag(OfferPolicy(), fake=fake)["selftests"] == []
    report = run_diag(OfferPolicy(gpus=("0",)), fake=fake, display=True)
    assert "drives a display" in report["selftests"][0]["error"] and fake.launches == 0


def test_diagnose_reports_jit_failure_verbatim():
    report = run_diag(OfferPolicy(gpus=("0",)), fake=FakeCuda(jit_error="ptxas: unsupported version"))
    assert "unsupported version" in report["selftests"][0]["error"]


def test_committed_ptx_targets_kepler_and_old_drivers():
    ptx = KERNEL.read_text()
    assert re.search(r"^\.version 3\.2$", ptx, re.M) and re.search(r"^\.target sm_30$", ptx, re.M)
    assert ".visible .entry matmul_i32(" in ptx and len(re.findall(r"^\s*\.param \.u(64|32) ", ptx, re.M)) == 4
    assert "Generated from agent/kernels/matmul_i32.ll" in ptx


def test_committed_ptx_is_reproducible_from_source():
    pytest.importorskip("llvmlite", reason="llvmlite==0.43.0 regenerates the PTX; optional")
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_ptx", KERNEL.parents[2] / "kernels" / "build_ptx.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    assert build.compile_ll(KERNEL.parents[2] / "kernels" / "matmul_i32.ll") == KERNEL.read_text()


def test_agent_versions_agree():
    text = (KERNEL.parents[2] / "pyproject.toml").read_text()
    assert f'version = "{__version__}"' in text
