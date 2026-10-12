"""Server verifiers and agent implementations must agree; both sides are checked here."""
from sharedcompute import workloads as server
from sharedcompute_agent import workloads as agent


def naive_matmul_sum(a, b, n):
    total = 0
    for i in range(n):
        for j in range(n):
            total += sum(a[i * n + k] * b[k * n + j] for k in range(n))
    return total


def test_hashchain_agent_output_verifies_on_server():
    params = server.issue_params(server.get("agent.hashchain"), hashchain_iterations=5_000)
    output = agent.hashchain(params, lambda: False)
    ok, detail = server.verify("agent.hashchain", params, output)
    assert ok, detail
    tampered = dict(output, digest="00" * 32)
    assert server.verify("agent.hashchain", params, tampered)[0] is False


def test_hashchain_params_are_fresh_per_assignment():
    w = server.get("agent.hashchain")
    a = server.issue_params(w, hashchain_iterations=10)
    b = server.issue_params(w, hashchain_iterations=10)
    assert a["seed"] != b["seed"]


def test_matmul_checksum_identity_matches_naive_product():
    for seed, n in [(1, 4), (12345, 9), (2**32 - 1, 16)]:
        a, b = agent.seeded_matrices(seed, n)
        assert server.matmul_checksum(seed, n) == naive_matmul_sum(a, b, n)


def test_matmul_values_are_small_integers_so_fp32_is_exact():
    a, b = agent.seeded_matrices(99, 32)
    assert set(a + b) <= set(range(-8, 9))
    # Worst-case dot product magnitude for the verification size stays far below 2**24.
    assert server.MATMUL_VERIFY_N * 64 < 2**24


def test_matmul_verifier_rejects_wrong_or_non_integer_checksums():
    params = {"seed": 7, "n": 8, "bench_n": 16, "repeats": 1}
    right = server.matmul_checksum(7, 8)
    assert server.verify("agent.matmul", params, {"checksum": right})[0]
    assert not server.verify("agent.matmul", params, {"checksum": right + 1})[0]
    assert not server.verify("agent.matmul", params, {"checksum": float(right)})[0]
    assert not server.verify("agent.matmul", params, {"checksum": True})[0]
    assert not server.verify("agent.matmul", params, "not a dict")[0]


def test_matmul_without_cuda_fails_cleanly(monkeypatch):
    monkeypatch.setattr(agent, "_torch", lambda: None)
    assert agent.capabilities() == ["agent.hashchain"]
    try:
        agent.matmul({"seed": 1, "n": 4, "bench_n": 4, "repeats": 1}, lambda: False)
    except RuntimeError as exc:
        assert "CUDA" in str(exc)
    else:
        raise AssertionError("matmul must refuse to run without CUDA")


def test_hashchain_honours_cancellation():
    import pytest

    with pytest.raises(agent.Cancelled):
        agent.hashchain({"seed": "00" * 32, "iterations": 100_000}, lambda: True)


def test_simulated_workloads_have_no_verifier():
    assert server.verify("sim.image", {}, {})[0] is False
    assert all(w.execution in ("simulated", "agent") for w in server.CATALOGUE.values())
