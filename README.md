# SharedCompute

**A marketplace that matches AI compute jobs to independently owned GPUs.** Providers run a small agent and
choose which devices to offer and at what hourly price. Customers submit jobs with requirements (VRAM,
compute capability, region) and a budget. A scheduler picks the cheapest compatible device. Completed,
verified work is settled with a 10% marketplace fee and a 90% provider share.

Founded by William and Stefan. This is an **early-stage MVP (v0.3)** developed in the open. It is not a
production service: there is no hosted marketplace to join, and no real money or cryptocurrency is involved.

> [!IMPORTANT]
> **Not yet verified on real hardware or real networks:**
> - **Real NVIDIA GPU execution has never been run.** The CUDA path (kernel, driver binding, detection) has only
>   been tested against a fake driver and synthetic `nvidia-smi` output.
> - **Cross-network operation has never been run.** Agents and the marketplace have only talked over
>   loopback (including TLS) on one machine.
>
> What *has* been verified on real hardware is listed in [docs/TEST_EVIDENCE.md](docs/TEST_EVIDENCE.md). The
> plan to close these gaps is [docs/TEST_RUNBOOK.md](docs/TEST_RUNBOOK.md) (Tests 2 and 3).

## What works today

| Area | Status |
|---|---|
| Accounts, marketplace, scheduler, job queue, budgets, retries, double-entry ledger, web UI | **Works**: automated tests and browser use |
| Provider agent executing a real, server-verified job (CPU workload) | **Works**, on an Intel MacBook Pro |
| Explicit device opt-in; refusal of display-driving and incompatible GPUs | **Works** (NVIDIA-specific detection unverified) |
| Agent ↔ marketplace over TLS with certificate verification | **Works on one machine** (loopback); not across networks |
| CUDA integer matmul on an NVIDIA GPU (PTX via the driver, server-verified result) | **Built, never run on a GPU** |
| "Did it run on the GPU?" | The server verifies results and cross-checks device identity and timing. A GPU claim is attested, **not proven** |
| Sample GPU listings, simulated workloads, account balances | **Simulated**: demo credits, no money moves |
| Customer-supplied code or containers, payments, payouts, crypto settlement | **Not built**, deliberately (see [architecture §7–8](docs/ARCHITECTURE.md)) |

## Quick start (development, macOS or Linux)

Requires Python 3.10+ for the marketplace. The provider agent alone needs Python 3.8+.

```bash
git clone https://github.com/crossainthero-lab/SharedCompute.git
cd SharedCompute                      # the default branch, feature/mvp-integration, is the current MVP
scripts/bootstrap-dev.sh --smoke      # venv, config, database, tests, end-to-end smoke test
./start.sh                            # http://127.0.0.1:8765 (loopback only)
```

Register in the browser, add demo credits under *Economics*, and submit jobs under *Jobs*. The four sample GPUs
(tagged `SIM`) are simulated so the marketplace works out of the box. The setup scripts never use `sudo`,
change drivers or offer any device. Details for every machine type, including unsupported ones:
[docs/SETUP_NEW_MACHINE.md](docs/SETUP_NEW_MACHINE.md).

Using Claude Code or another coding agent? Open the folder and say *"Set up this SharedCompute repo on this
machine."* [CLAUDE.md](CLAUDE.md) holds the agents' instructions.

### Try the provider agent on your own machine (CPU only)

```bash
# In the UI: Provide -> Add hardware -> Connect a machine, then save the one-time token:
umask 077; printf '%s' 'scn_…' > /tmp/sc-node.token
.venv/bin/python -m sharedcompute_agent run --token-file /tmp/sc-node.token --offer-cpu
```

Submit a **Pipeline check (CPU)** job. It runs on your CPU, and the server recomputes the result before billing.

### NVIDIA GPU providers (help wanted for testing)

```bash
scripts/bootstrap-agent.sh            # agent only: Python 3.8+, no CUDA toolkit or PyTorch; offers nothing
~/.local/share/sharedcompute/agent-venv/bin/python -m sharedcompute_agent diagnose --gpu 0 --selftest
```

- **Supported in principle:** NVIDIA Kepler (compute capability 3.0) or newer, with a driver supporting CUDA 5.5+.
- **Not supported:** Fermi, nouveau, AMD, Intel and Apple GPUs. These are detected and refused with a reason.
- **Nothing is used unless you choose it**, and a GPU that drives a display is refused unless you explicitly allow it.

See [docs/HARDWARE_TESTING.md](docs/HARDWARE_TESTING.md). Results from real cards are the most valuable
contribution right now.

## How it works

```
Browser / API ──▶ FastAPI /api/v1 ──▶ scheduler + queue + double-entry ledger ──▶ SQLite (versioned migrations)
                                   ▲
provider agent ── HTTPS, outbound only, per-node token ──▶ /api/agent/v1 ──▶ result verifiers + execution evidence
   └─ offers only owner-chosen devices: NVIDIA GPU (CUDA driver API + PTX) and/or CPU
```

- Budgets are held at submission. Completed jobs are charged by **server-measured** runtime (never above
  the quote), then split 90/10. Failures and cancellations cost nothing.
- Agents run only fixed workload templates whose output the server can check: a SHA-256 chain recomputed
  on the CPU, and a GPU matrix multiply with an exact checksum and Merkle-committed rows challenged at random.
- Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

```bash
.venv/bin/python -m pytest                        # 219 tests (marketplace + agent)
.venv/bin/python scripts/smoke_local.py           # real agent + throwaway marketplace, CPU job
sh scripts/check-repo-hygiene.sh                  # no secrets, databases or local files tracked
```

- Expect **1 skipped** test unless the optional `llvmlite==0.43.0` is installed. That test regenerates the GPU
  kernel's PTX from source; skipping it says nothing about hardware.
- CI runs:
  - the suite on Linux and macOS (Python 3.10, 3.12);
  - the agent alone on Python 3.8;
  - the PTX reproducibility check;
  - fresh-runner bootstrap plus smoke tests;
  - the hygiene check.

## Current limitations

- No NVIDIA GPU or second-network test has run yet (see the notice above).
- Single-process SQLite. No email verification, password reset or 2FA. Rate limiting is per process.
- Hardware reports are self-attested by providers. GPU execution is cross-checked, not cryptographically proven.
- No real payments, payouts or KYC. No hosted service. Do not expose a marketplace publicly without TLS (the
  server refuses to) and the checklist in [docs/REMOTE_CONNECTIVITY.md](docs/REMOTE_CONNECTIVITY.md).

## Contributing

- **Hardware test reports** are the most useful contribution: run the [runbook](docs/TEST_RUNBOOK.md) on an
  older NVIDIA card and open an issue with the results template from
  [docs/HARDWARE_TESTING.md](docs/HARDWARE_TESTING.md). Redact tokens and hostnames.
- **Issues** for bugs, questions and design feedback are welcome.
- **Pull requests:** please open an issue first. No open-source licence has been granted yet (see below), so
  code contributions can't be accepted until the founders choose one.
- Development rules (tests for every change, migrations, the PTX build, honesty about verification) are in
  [CLAUDE.md](CLAUDE.md) §9.
- **Security issues:** please report privately, as described in [SECURITY.md](SECURITY.md).

## Licence

**No licence has been chosen yet.** The packages are marked `Proprietary`, so the code is visible but all
rights are reserved: you may read it, but not reuse, modify or redistribute it. Choosing a licence is an open
decision for the founders.

## Documentation

[docs/SETUP_NEW_MACHINE.md](docs/SETUP_NEW_MACHINE.md) · [docs/TEST_RUNBOOK.md](docs/TEST_RUNBOOK.md) ·
[docs/TEST_EVIDENCE.md](docs/TEST_EVIDENCE.md) · [docs/HARDWARE_TESTING.md](docs/HARDWARE_TESTING.md) ·
[docs/REMOTE_CONNECTIVITY.md](docs/REMOTE_CONNECTIVITY.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[CLAUDE.md](CLAUDE.md) · [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md)

Branches: `feature/mvp-integration` (default, current MVP) and `main` (the original v0.1 draft, kept as the
history baseline).
