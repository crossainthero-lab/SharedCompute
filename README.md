# SharedCompute

A marketplace that matches AI compute jobs to independently owned GPUs. Providers run a small agent and
choose which devices to offer and at what hourly price. Customers submit jobs with requirements and a
budget. A scheduler picks the cheapest compatible device. Completed, verified work is settled with a 10%
marketplace fee and a 90% provider share.

Founded by William and Stefan. **v0.3: local MVP, preparing for real low-end GPUs.**

> **Status:**
> - **Works:** the marketplace, scheduler, ledger, UI and provider agent. Real jobs have run on a real
>   machine's CPU.
> - **Built but unverified:** the GPU path (a CUDA kernel loaded through the NVIDIA driver, server-side
>   verification, execution evidence) has been tested only against a fake driver. It **has not yet run on an
>   NVIDIA GPU**.
> - **Simulated:** balances are demo credits; no money moves.

## Repository and branches

- Private repo: **https://github.com/crossainthero-lab/SharedCompute** (request access from the founders).
- **`feature/mvp-integration` is the current development branch and the repository default.** `main` holds the
  original v0.1 founders' draft (history baseline). Nothing has been merged into `main`.

```bash
gh auth login                                       # once per machine, if needed
gh repo clone crossainthero-lab/SharedCompute       # or: git clone https://github.com/crossainthero-lab/SharedCompute.git
cd SharedCompute
git checkout feature/mvp-integration
```

## Set up a new machine

Tell Claude Code (or another coding agent) in the cloned folder:

> **Set up this SharedCompute repo on this machine.**

It will read [CLAUDE.md](CLAUDE.md), identify the machine's role and follow
[docs/SETUP_NEW_MACHINE.md](docs/SETUP_NEW_MACHINE.md). Or do it yourself:

| Machine | Command | Result |
|---|---|---|
| Development (macOS or Linux) | `scripts/bootstrap-dev.sh` | `.venv`, `.env`, database, diagnostics, tests |
| GPU provider (Linux + NVIDIA) | `scripts/bootstrap-agent.sh` | Agent-only venv and config that offers nothing yet, plus diagnostics |
| Any (read-only check) | `scripts/doctor.sh` | PASS/WARN/FAIL report; changes nothing |

The scripts are idempotent. They never use `sudo`, change drivers, install CUDA/PyTorch, delete data or
offer any device. Unsupported setups (Windows, nouveau, Fermi, non-NVIDIA GPUs) are explained in
SETUP_NEW_MACHINE.md §D.

## Run the marketplace

```bash
./start.sh                   # http://127.0.0.1:8765, loopback only; bootstraps on first run
```

Register in the browser, add demo credits under *Economics*, and submit jobs under *Jobs*. To see
platform-wide economics, run `.venv/bin/python -m sharedcompute make-operator you@example.com`. Settings for
this machine go in `.env` (from [.env.example](.env.example)). The database is `data/sharedcompute.sqlite3`
(git-ignored), upgraded in place with an automatic backup.

## Run the provider agent

```bash
# Try it on this machine (CPU only), against the local marketplace:
#   Provide -> Add hardware -> Connect a machine, then save the token:
umask 077; printf '%s' 'scn_…' > /tmp/sc-node.token
.venv/bin/python -m sharedcompute_agent run --token-file /tmp/sc-node.token --offer-cpu

# A real provider machine (after scripts/bootstrap-agent.sh):
~/.local/share/sharedcompute/agent-venv/bin/python -m sharedcompute_agent diagnose --gpu 0 --selftest
~/.local/share/sharedcompute/agent-venv/bin/sharedcompute-agent run   # reads ~/.config/sharedcompute/agent.env
```

- **Nothing is used unless the owner chooses it** (`--gpu N`, `--offer-cpu`, or the agent config). A GPU that drives a
  display is refused unless you add `--allow-display-gpu`.
- **Old hardware:** NVIDIA Kepler (CC 3.0) or newer, a driver supporting CUDA 5.5+, 64 MB free VRAM. No CUDA
  toolkit or PyTorch: the kernel ships as PTX that the driver compiles.
- **Remote marketplaces must be `https://`.** The agent only connects outbound and verifies certificates
  (`--ca-file` for a private CA). Tailscale is the recommended way to join separate networks:
  [docs/REMOTE_CONNECTIVITY.md](docs/REMOTE_CONNECTIVITY.md).

## Tests

```bash
.venv/bin/python -m pytest                           # 219 tests (marketplace + agent)
.venv/bin/python scripts/smoke_local.py              # real agent + throwaway marketplace, CPU job (Test 1)
.venv/bin/python scripts/smoke_local.py --gpu 0      # + real CUDA job (NVIDIA Linux only, explicit choice)
sh scripts/check-repo-hygiene.sh                     # no secrets/databases tracked or in history
```

**Python versions.**
- Marketplace: Python 3.10+ (tested on 3.12 and 3.14).
- Agent: Python 3.8+ (tested on 3.8).
- One test regenerates the PTX from source. It needs the optional `llvmlite==0.43.0`, which has wheels only for
  some Python versions and platforms. Without it, that test is **skipped**: expect "1 skipped", e.g. on Python 3.14.

A skipped or simulated test is not evidence that hardware works. What has really been verified is listed in
[docs/TEST_EVIDENCE.md](docs/TEST_EVIDENCE.md). The three-stage hardware plan (Mac only → real NVIDIA GPU →
two networks) is in [docs/TEST_RUNBOOK.md](docs/TEST_RUNBOOK.md).

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs:
- the test suite on Linux and macOS;
- the agent alone on Python 3.8;
- the PTX reproducibility check;
- bootstrap + smoke on fresh runners;
- the repository hygiene check.

## Architecture in brief

```
Browser / API ──▶ FastAPI /api/v1 ──▶ scheduler + queue + double-entry ledger ──▶ SQLite (versioned migrations)
                                   ▲
provider agent ── HTTPS, outbound, node token ──▶ /api/agent/v1 ──▶ verifiers + execution evidence
   └─ offered devices only: NVIDIA GPU (CUDA driver API + PTX) and/or CPU
```

Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Directory map, commands and rules for agents:
[CLAUDE.md](CLAUDE.md).

## What works, what is simulated, what is not built

| Area | State |
|---|---|
| Accounts, marketplace, scheduler, queue, budgets, retries, ledger, UI | **Works** (tested; used in a browser) |
| Real job executed by the agent and verified (CPU workload) | **Works** on an Intel MacBook Pro |
| Device opt-in, display-GPU guard, compatibility checks, graceful rejection | **Works**; NVIDIA-specific detection unverified |
| TLS between agent and marketplace | **Works on loopback**; not yet across two networks |
| CUDA matmul on an NVIDIA GPU | **Built, never run on hardware** |
| "Did it run on the GPU?" | Server verifies the result and cross-checks identity and timing; the GPU claim is attested, not proven |
| Sample network, simulated workloads, balances | **Simulated** by design |
| Customer containers, real payments, payouts, crypto settlement | **Not built** (deliberately) |

## Current limitations before real users

- No NVIDIA hardware or second-network test has run (Tests 2 and 3 in the runbook).
- No real payments or payouts. No email verification or password reset. Single-process SQLite.
- Providers' hardware reports are self-attested. GPU execution is cross-checked, not cryptographically proven.
- Agents run only fixed workload templates; customer code needs sandboxing first (architecture §7).

## Documentation

[CLAUDE.md](CLAUDE.md) (agents' canonical instructions) ·
[docs/SETUP_NEW_MACHINE.md](docs/SETUP_NEW_MACHINE.md) · [docs/TEST_RUNBOOK.md](docs/TEST_RUNBOOK.md) ·
[docs/HARDWARE_TESTING.md](docs/HARDWARE_TESTING.md) · [docs/REMOTE_CONNECTIVITY.md](docs/REMOTE_CONNECTIVITY.md) ·
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/TEST_EVIDENCE.md](docs/TEST_EVIDENCE.md) ·
[PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md)
