"""Server verifiers and agent implementations must agree; both sides are checked here."""
import pytest

from sharedcompute import workloads as server
from sharedcompute_agent import workloads as agent


def test_hashchain_agent_output_verifies_on_server():
    params = server.issue_params(server.get("agent.hashchain"), hashchain_iterations=5_000)
    output = agent.hashchain(params, lambda: False)
    ok, detail = server.verify("agent.hashchain", params, output)
    assert ok, detail
    assert server.verify("agent.hashchain", params, dict(output, digest="00" * 32))[0] is False


def test_params_are_fresh_per_assignment():
    for wid in ("agent.hashchain", "agent.cuda_matmul"):
        a = server.issue_params(server.get(wid), hashchain_iterations=10)
        b = server.issue_params(server.get(wid), hashchain_iterations=10)
        assert a["seed"] != b["seed"]


def test_lcg_and_matrices_agree():
    assert server.lcg_values(42, 1000) == agent.lcg_values(42, 1000)
    a, b = agent.seeded_matrices(42, 10)
    values = server.lcg_values(42, 200)
    assert (a, b) == (values[:100], values[100:])
    assert set(a + b) <= set(range(-8, 9))


def test_checksum_identity_matches_full_product():
    for seed, n in [(1, 4), (12345, 9), (2**32 - 1, 16)]:
        a, b = agent.seeded_matrices(seed, n)
        assert server.matmul_checksum(seed, n) == sum(agent.cpu_matmul(a, b, n))
        for i in range(n):
            assert server.matmul_row(a, b, n, i) == agent.cpu_matmul(a, b, n)[i * n:(i + 1) * n]


def test_int32_never_overflows_at_production_size():
    # |A[i,k] * B[k,j]| <= 64, so a dot product of length n stays within +-64n.
    assert 64 * server.MATMUL_N < 2**31


def test_merkle_and_challenge_primitives_agree():
    rows = [[i * 3 + j for j in range(5)] for i in range(7)]
    assert all(server.merkle_leaf(i, r) == agent.merkle_leaf(i, r) for i, r in enumerate(rows))
    assert server.challenge_rows("ab" * 32, "cd" * 16, 4, 7) == agent.challenge_rows("ab" * 32, "cd" * 16, 4, 7)
    assert len(set(server.challenge_rows("ab" * 32, "cd" * 16, 10, 7))) == 7  # capped at n, no repeats


def test_agent_matmul_commitment_verifies_on_server(monkeypatch):
    monkeypatch.setattr(server, "MATMUL_N", 24)
    params = server.issue_params(server.get("agent.cuda_matmul"), hashchain_iterations=0)
    a, b = agent.seeded_matrices(params["seed"], params["n"])
    out = agent.commit(agent.cpu_matmul(a, b, params["n"]), params["n"], params["nonce"], params["samples"])
    ok, detail = server.verify("agent.cuda_matmul", params, out)
    assert ok and "4 challenged rows" in detail


@pytest.mark.parametrize("bad", [{"checksum": 1.0}, {"checksum": True}, {"merkle_root": "zz"}, {"samples": "x"}])
def test_matmul_verifier_rejects_malformed_output(monkeypatch, bad):
    monkeypatch.setattr(server, "MATMUL_N", 8)
    params = server.issue_params(server.get("agent.cuda_matmul"), hashchain_iterations=0)
    a, b = agent.seeded_matrices(params["seed"], 8)
    out = agent.commit(agent.cpu_matmul(a, b, 8), 8, params["nonce"], params["samples"])
    out.update(bad)
    assert server.verify("agent.cuda_matmul", params, out)[0] is False
    assert server.verify("agent.cuda_matmul", params, "not a dict")[0] is False


def test_requirements_agree_between_server_and_agent():
    for wid, req in agent.REQUIREMENTS.items():
        w = server.get(wid)
        assert w.device == req["device"]
        if w.min_compute_capability:
            assert server.parse_cc(w.min_compute_capability) == req["min_compute_capability"]
            assert w.min_cuda_driver == req["min_cuda_driver"] and w.min_vram_mb == req["min_vram_mb"]
    assert {w.id for w in server.CATALOGUE.values() if w.execution == "agent"} == set(agent.REQUIREMENTS)


def device(**kw):
    base = {"device_type": "gpu", "cuda": 1, "compute_capability": "6.1", "cuda_driver_version": 12020, "vram_mb": 4096}
    base.update(kw)
    return base


def test_incompatibility_reasons_for_old_hardware():
    w = server.get("agent.cuda_matmul")
    assert server.incompatibilities(w, device()) == []
    assert server.incompatibilities(w, device(compute_capability="3.0", cuda_driver_version=5050, vram_mb=64)) == []
    assert server.incompatibilities(w, device(compute_capability="2.1")) == ["compute capability 2.1 is below 3.0"]
    assert server.incompatibilities(w, device(cuda_driver_version=5000)) == ["driver supports CUDA 5.0; needs 5.5 or newer"]
    assert server.incompatibilities(w, device(cuda=0)) == ["needs an NVIDIA GPU with a working CUDA driver"]
    assert server.incompatibilities(w, device(device_type="cpu")) == ["needs a GPU device"]
    assert server.incompatibilities(w, device(compute_capability=None)) == ["compute capability unknown"]
    assert server.parse_cc("10.0") == (10, 0) and server.parse_cc("6") is None


def test_simulated_workloads_have_no_verifier():
    assert server.verify("sim.image", {}, {})[0] is False
