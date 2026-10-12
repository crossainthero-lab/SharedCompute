# CLAUDE.md: SharedCompute (canonical instructions for coding agents)

This file is the single source of truth for any Claude Code session, or other coding agent, working in this
repository. `AGENTS.md` points here. Read all of it before changing anything. If something here conflicts
with what you observe, trust the code and tests, fix this file, and say so.

## 1. What SharedCompute is

A marketplace that matches AI compute jobs to independently owned GPUs. Founders: William and Stefan.

- **Providers** run a small agent on their machine. It reports hardware and executes allowlisted,
  verifiable workloads on the devices the owner explicitly offers.
- **Customers** submit jobs with requirements (workload, VRAM, compute capability, region) and a budget.
- **The scheduler** matches each job to the cheapest compatible, available device within budget.
- **The ledger** (double-entry, simulated credits) holds the budget at submission. On verified completion it
  charges the measured cost and splits it 90% provider / 10% marketplace. Failures and cancellations
  release the hold.

It is a **local MVP** (v0.3): no real money, no token, no customer-supplied code, no production deployment.

## 2. Branches and repository

- GitHub: `crossainthero-lab/SharedCompute` (**private**). The default branch is `feature/mvp-integration`.
- **`feature/mvp-integration` is the current development branch.** `main` holds the untouched v0.1 founders'
  draft, the baseline of the history.
- Never merge into `main`, force-push, rewrite published history or deploy without explicit approval from the
  user. Commit and push only when asked. Develop on a branch off `feature/mvp-integration`.

## 3. First steps in a new session

1. Identify the machine and its role:
   ```bash
   uname -s; uname -m; (sw_vers 2>/dev/null || cat /etc/os-release 2>/dev/null | head -3)
   command -v nvidia-smi && nvidia-smi --query-gpu=index,name,driver_version,display_active --format=csv
   git rev-parse --abbrev-ref HEAD; git status --short | head
   scripts/doctor.sh          # read-only health check; safe on any machine
   ```
2. Decide the role from the user's request and the hardware (ask if unclear):

   | Role | Typical machine | Set up with |
   |---|---|---|
   | A. Development: macOS | William's Intel MacBook Pro (Intel UHD 630 + AMD Radeon Pro 5300M, no CUDA) | `scripts/bootstrap-dev.sh` |
   | B. Development: Linux | Ubuntu/Debian workstation, GPU optional | `scripts/bootstrap-dev.sh` |
   | C. Compute provider: Linux + NVIDIA | Older/low-end NVIDIA card (Kepler CC 3.0 or newer) | `scripts/bootstrap-agent.sh` |
   | D. Unsupported / limited | Windows, nouveau, Fermi, AMD/Intel/Apple GPUs | Explain limits; CPU-only agent at most |

3. Follow **[docs/SETUP_NEW_MACHINE.md](docs/SETUP_NEW_MACHINE.md)** for that role, end to end. When the user says
   "set up this repo on this machine", that document is the procedure.
4. Verify with the relevant test in **[docs/TEST_RUNBOOK.md](docs/TEST_RUNBOOK.md)** and report honestly.

## 4. Non-negotiable rules

**Hardware and the provider's machine**
- Never offer, select or use a GPU or CPU unless the owner explicitly chooses it (`--gpu N`, `--offer-cpu`, or
  `SHAREDCOMPUTE_GPUS` / `SHAREDCOMPUTE_OFFER_CPU` in their agent config). Never pick one on their behalf.
- Never offer or self-test a GPU that drives a display unless the owner explicitly says so (`--allow-display-gpu`).
- Never install, remove or change GPU drivers, kernels or kernel modules. Never install the CUDA toolkit,
  PyTorch or other heavyweight frameworks. Only recommend these, and the user runs them.
- Never use `sudo` or system package managers without asking first. Never modify macOS system Python.
- Never start the agent as a background service or at boot unless asked.

**Network and exposure**
- The marketplace binds `127.0.0.1` by default. Never expose it publicly, create tunnels (ngrok, Cloudflare
  Tunnel, `tailscale funnel`) or open firewall ports without explicit approval. Tailscale (private tailnet,
  `tailscale serve`) is the recommended, opt-in way to connect separate networks.
- Remote agents must use `https://`. Never weaken TLS checks or disable certificate verification.

**Secrets and data**
- Never commit or print node tokens (`scn_…`), session tokens (`scs_…`), `.env`, TLS keys, wallets or
  databases. Machine settings live outside Git (`.env`, `~/.config/sharedcompute/agent.env`, `node.token`, mode 600).
- Never delete `data/` or a database. Upgrade with `python -m sharedcompute migrate`, which backs up first.
  Experiments use temporary databases (`scripts/smoke_local.py` does).
- Don't delete a broken `.venv`. The bootstrap scripts move it aside (`.venv.broken-<time>`).
- Run `sh scripts/check-repo-hygiene.sh` before any push.

**Honesty**
- Never claim NVIDIA execution, a hardware test or a two-network test passed without real evidence. Record
  results in `docs/TEST_EVIDENCE.md` and `docs/hardware-results/`. A skipped or simulated test proves nothing
  about hardware. The fake CUDA driver (`agent/tests/fake_cuda.py`) validates marshalling, not the GPU kernel.

## 5. Architecture

```
Browser / API client ──▶ FastAPI /api/v1 ──▶ services (accounts, nodes, jobs, scheduler, ledger, economics) ──▶ SQLite
                                                       ▲                                              (migrations/)
Provider agent ── HTTPS, outbound, Bearer scn_ token ──▶ /api/agent/v1 ──▶ agent_protocol ──▶ verifiers ─▶ ledger
  ├─ gpu.py: NVML / nvidia-smi / CUDA driver probe / macOS discovery; CPU as its own device
  ├─ policy.py: opt-in (nothing offered by default; display GPUs refused)
  ├─ cuda.py: ctypes CUDA driver API; loads kernels/matmul_i32.ptx via driver JIT (no toolkit)
  └─ workloads.py: agent.hashchain (CPU), agent.cuda_matmul (GPU; explicit, labelled CPU fallback)
```

**Job lifecycle:** `pending → assigned → running → completed | failed | cancelled`. A ticker thread (1 s)
requeues jobs from silent agents (heartbeat TTL 30 s), unclaimed assignments (90 s) and over-time jobs, runs
the simulator, then schedules. Interrupted attempts requeue up to 3 times. A graceful hardware rejection
(`fail` with `code: "incompatible"`) requeues without using an attempt.

**Agent protocol** (`sharedcompute/services/agent_protocol.py`; the agent always initiates):
- `hello` reports the devices with their opt-in state and workloads.
- `heartbeat` sends metrics every 10 s and receives the job ids to cancel.
- `jobs/claim` takes the next assigned job.
- `jobs/{id}/complete` verifies the result, records execution evidence and settles at **server-measured** runtime.
- `jobs/{id}/fail` reports a failure.

**Verification and evidence:**
- `agent.hashchain`: the server recomputes the digest.
- `agent.cuda_matmul`: the server checks an exact checksum plus Merkle-committed rows, challenged via
  `sha256(root‖nonce)` and recomputed.
- Evidence levels: `gpu_attested`, `cpu_verified`, `cpu_fallback` (never billed as GPU), `inconsistent`,
  `failed_verification`, `simulated`. "GPU attested" means the result is verified and the device identity and
  timing are cross-checked. It is **not** proof that a GPU ran it.

**Ledger** (`services/ledger.py`): accounts `user:N:available|held|earnings`, `platform:revenue`,
`platform:demo_funding`. Entries: `demo_topup`, `hold`, `charge`, `release`. Every entry sums to zero, and
tests assert this.

### Directory map and entry points

| Path | What |
|---|---|
| `sharedcompute/__main__.py` | `python -m sharedcompute serve / migrate / add-user / make-operator` |
| `sharedcompute/api/` | FastAPI app (`app.py`), user routes, agent routes, request schemas |
| `sharedcompute/services/` | Domain logic. Each function takes an open connection; callers own the transaction |
| `sharedcompute/migrations/` | Numbered SQL migrations (`PRAGMA user_version`). Never edit an applied one |
| `sharedcompute/workloads.py` | Workload catalogue, hardware requirements, server-side verifiers |
| `sharedcompute/web/` | No-build ES-module UI, strict CSP (no inline scripts or styles) |
| `agent/` | Separately installable agent (`agent/pyproject.toml`, Python 3.8+, no dependencies) |
| `agent/kernels/` | `matmul_i32.ll` (source) and `build_ptx.py` (needs `llvmlite==0.43.0`) → `sharedcompute_agent/kernels/*.ptx` |
| `scripts/` | `bootstrap-dev.sh`, `bootstrap-agent.sh`, `doctor.sh`, `smoke_local.py`, `make-dev-tls.sh`, `check-repo-hygiene.sh` |
| `tests/` (server), `agent/tests/` (agent only) | pytest. The agent tests must not import server code |
| `docs/` | Architecture, setup, runbook, hardware testing, remote connectivity, test evidence |

## 6. Everyday commands

```bash
scripts/bootstrap-dev.sh                           # dev setup (idempotent); --skip-tests, --smoke
./start.sh                                         # marketplace on http://127.0.0.1:8765
.venv/bin/python -m pytest                         # all tests (server + agent)
.venv/bin/python scripts/smoke_local.py            # real agent + temporary marketplace, CPU job (Test 1)
.venv/bin/python scripts/smoke_local.py --gpu 0    # + real CUDA job on GPU 0 (NVIDIA Linux, explicit)
.venv/bin/python -m sharedcompute migrate          # create/upgrade the DB (backup first)
.venv/bin/python -m sharedcompute add-user EMAIL --name NAME [--operator]
.venv/bin/python -m sharedcompute_agent diagnose [--gpu N --selftest]
sh scripts/check-repo-hygiene.sh                   # before pushing
scripts/doctor.sh                                  # read-only health check
```

Configuration: `.env` (from `.env.example`) for the marketplace; `~/.config/sharedcompute/agent.env` (from
`agent/agent.env.example`) for the agent. The environment and flags override both.

## 7. Capability status (keep in sync with docs/TEST_EVIDENCE.md)

| Capability | Status |
|---|---|
| Marketplace, scheduler, queue, retries, ledger, UI, auth | **Real**: tested; used in a browser on the Mac |
| Agent on a real machine running a verified CPU job | **Real**: Intel MacBook Pro |
| Device opt-in, non-NVIDIA refusal | **Real** on macOS |
| TLS between agent and server | **Real on loopback** (private CA); not across networks |
| Sample listings, `sim.*` workloads, balances | **Simulated** by design |
| NVIDIA detection (NVML, nvidia-smi, CUDA probe), display-GPU detection | **Unverified**: synthetic data and a fake driver only |
| CUDA kernel (PTX JIT) and `agent.cuda_matmul` on a GPU | **Unverified**: never run on NVIDIA hardware |
| Two separate networks | **Unverified** |
| Customer containers, real payments, payouts, crypto | **Not built** (deliberately) |

## 8. Diagnosing problems without causing damage

- Start with `scripts/doctor.sh` and `python -m sharedcompute_agent diagnose`. Both are read-only.
- Reproduce in isolation with `scripts/smoke_local.py --keep`: temporary database, random port, ignores `.env`
  and agent config, and keeps the logs.
- Server problems: check the server log, `python -m sharedcompute migrate --check`, and `/api/health`.
- Agent problems: run with `-v`. A 401 means the token was revoked or is wrong (issue a new one in the UI). TLS
  errors mean a hostname mismatch or a missing `--ca-file`. "Refusing plain HTTP" means use `https://`.
- GPU problems: run `diagnose --gpu N --selftest` and keep the full output. JIT errors include the driver's log.
  See the troubleshooting table in [docs/HARDWARE_TESTING.md](docs/HARDWARE_TESTING.md).
- Never fix things by deleting data, weakening checks, skipping tests or editing applied migrations.

## 9. Development conventions

- Match the surrounding style: small functions, explicit validation with field-specific errors, integer cents,
  no new dependencies without a reason (the agent stays dependency-free and Python 3.8-compatible).
- Every behaviour change gets tests. Run `pytest` (and the hygiene script) before committing.
- Schema changes go in a new `sharedcompute/migrations/NNNN_*.sql` plus a migration test.
- Kernel changes: edit the `.ll`, run `python agent/kernels/build_ptx.py`, and commit both (CI checks freshness).
- The UI uses the `html` tagged template (auto-escaping) and `ui.raw` only for trusted markup. No inline styles (CSP).
- Commit messages: imperative summary, explanatory body, and the co-author trailer the session specifies.
- Update `docs/TEST_EVIDENCE.md` whenever something is verified on real hardware.

## 10. Documentation map

- [README.md](README.md): overview, quick start, status
- [docs/SETUP_NEW_MACHINE.md](docs/SETUP_NEW_MACHINE.md): per-role setup procedure (A–D)
- [docs/TEST_RUNBOOK.md](docs/TEST_RUNBOOK.md): Test 1 (Mac only), Test 2 (real NVIDIA), Test 3 (two networks)
- [docs/HARDWARE_TESTING.md](docs/HARDWARE_TESTING.md): GPU compatibility, self-test, troubleshooting
- [docs/REMOTE_CONNECTIVITY.md](docs/REMOTE_CONNECTIVITY.md): Tailscale, private CA, VPS + Caddy
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): full design, data model, security model, roadmap
- [docs/TEST_EVIDENCE.md](docs/TEST_EVIDENCE.md): what has actually been verified, and where
- [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md): founders' original product blueprint
