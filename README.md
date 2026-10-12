# SharedCompute

A marketplace that matches AI compute jobs to independently owned GPUs. Providers list hardware and set
an hourly price; customers submit jobs with requirements and a budget; a scheduler picks the cheapest
compatible device; completed, verified work is settled with a 10% marketplace fee and a 90% provider share.

Founded by William and Stefan. **v0.3: local MVP, preparing for real low-end GPUs.** It builds on the v0.1
founders' draft (see git history and [`PROJECT_BLUEPRINT.md`](PROJECT_BLUEPRINT.md)).

> **Status in one line:** the marketplace, scheduler, ledger, UI and provider agent work, and real jobs have
> run on a real machine's CPU. The GPU path (a CUDA kernel loaded through the NVIDIA driver, server-side
> verification, execution evidence) is built and tested against a fake driver, but **has not yet run on an
> NVIDIA GPU**. Balances are simulated credits; no money moves.

Read next: [what was really tested](docs/TEST_EVIDENCE.md) ·
[connect your first older GPU](docs/HARDWARE_TESTING.md) ·
[connect across networks](docs/REMOTE_CONNECTIVITY.md) · [architecture](docs/ARCHITECTURE.md)

## Quick start (marketplace, local)

Requires Python 3.10+.

```bash
./start.sh                 # first run creates .venv and installs dependencies
```

Open http://127.0.0.1:8765. Data lives in `data/sharedcompute.sqlite3` (git-ignored). Existing v0.2
databases are migrated in place on startup.

Manual equivalent: `python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]' && python -m sharedcompute serve`.
To see platform-wide economics, register in the UI, then run `python -m sharedcompute make-operator you@example.com`.

## Try the whole flow on one machine (no GPU needed)

1. **Browse** the marketplace. Four sample GPUs (tagged `SIM`) are always available.
2. **Create an account**, open *Economics* and add demo credits (simulated; no payment is taken).
3. **Submit a simulated job** on *Jobs*. The live quote shows the matched device, price and fee split, or why
   the job would queue. The budget is held until the job ends.
4. **Connect this machine as a provider** (CPU only): *Provide → Add hardware → Connect a machine*, copy the
   token, then:

   ```bash
   umask 077; printf '%s' 'scn_…' > ~/.sharedcompute-node-token
   python -m sharedcompute_agent run --token-file ~/.sharedcompute-node-token --offer-cpu
   ```

   Submit a **Pipeline check (CPU)** job. It really runs on your CPU, the server recomputes the result, and
   the job shows **CPU · VERIFIED** with server-measured billing.
5. **Economics** shows balances, ledger entries and, for operators, volume, revenue and ledger integrity.

## Provider agent

The agent is a separate, dependency-free package (`agent/`, Python 3.8+) so provider machines don't install
the server:

```bash
pip install ./agent
python -m sharedcompute_agent diagnose                      # what was detected and what each device can run
python -m sharedcompute_agent diagnose --gpu 0 --selftest   # run the real CUDA kernel on GPU 0 and check it
python -m sharedcompute_agent run --server https://… --token-file … --gpu 0 [--offer-cpu]
```

- **Nothing is used unless you choose it.** `--gpu INDEX|UUID` offers a GPU and `--offer-cpu` the CPU. A GPU
  that drives a display is refused unless you add `--allow-display-gpu`.
- **Old hardware:** NVIDIA Kepler (compute capability 3.0) or newer, any driver supporting CUDA 5.5+, 64 MB
  free VRAM. Fermi, AMD, Intel and Apple GPUs are refused with a reason. No CUDA toolkit or PyTorch needed:
  the kernel ships as PTX that the driver compiles.
- **Remote marketplaces must use HTTPS.** The agent refuses plain HTTP to anything but localhost. It verifies
  certificates (system store, or `--ca-file` for a private CA) and only ever connects outbound.

Step-by-step for the first real card: [docs/HARDWARE_TESTING.md](docs/HARDWARE_TESTING.md).

## Architecture

```
Browser / API client ──▶ FastAPI (/api/v1) ──▶ scheduler + queue ──▶ SQLite (versioned migrations)
                                   ▲                    ▲
  provider agents ── HTTPS, outbound, node token ──▶ /api/agent/v1 ──▶ verifiers + evidence ──▶ ledger
        └─ devices: offered GPUs (CUDA driver API + PTX) and/or CPU
```

- `sharedcompute/`: marketplace. `api/` (HTTP), `services/` (accounts, nodes, jobs, scheduler, agent protocol,
  ledger, economics), `migrations/`, `workloads.py` (catalogue, hardware requirements, verifiers), `web/` (UI).
- `agent/`: provider agent. `gpu.py` (NVML / nvidia-smi / CUDA / macOS discovery), `cuda.py` (ctypes driver
  binding), `policy.py` (opt-in), `workloads.py`, `diagnose.py`, `kernels/` (LLVM IR source plus generated PTX).
- `scripts/`: `make-dev-tls.sh` (private CA for pilots), `check-repo-hygiene.sh` (secrets / artefacts check).

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). API reference at http://127.0.0.1:8765/api/docs (local only).

## What works, what is simulated, what is not built

| Area | State | Notes |
|---|---|---|
| Accounts, sessions, ownership checks | Works | PBKDF2, HttpOnly cookie (Secure when exposed), CSRF header, rate-limited login, `add-user` CLI |
| Marketplace, scheduler, queue, budgets, retries | Works | Cheapest compatible device within budget; FIFO; 3 attempts |
| Hardware compatibility | Works | Workload requirements (CC, CUDA driver, VRAM) checked by server and agent; job-level min CC; quotes explain mismatches |
| Graceful rejection | Works | A device that declines a job (e.g. low free VRAM) requeues it without costing an attempt or a provider strike |
| Double-entry ledger, 10% / 90% split | Works | Invariants tested after every scenario |
| Real execution on a provider | Works (CPU) | `agent.hashchain` executed by the real agent on a Mac, verified, billed |
| Device opt-in, display-GPU guard | Works | Verified on the Mac; NVIDIA `display_active` reading not yet seen on real hardware |
| CUDA matmul on a GPU | **Built, untested on hardware** | LLVM-generated PTX (sm_30), ctypes driver API, Merkle-committed result with challenged rows |
| "Did it run on the GPU?" | Partial by design | Server verifies the result and cross-checks device identity and timing; the GPU claim is attested, not proven. CPU fallback is recorded and never billed as GPU |
| NVIDIA detection (NVML, nvidia-smi, CUDA probe) | **Built, untested on hardware** | Tested with synthetic output and a fake driver |
| TLS for remote agents | Works on loopback | Private-CA TLS tested end to end; not yet across two networks |
| Sample network, simulated workloads, balances | **Simulated** | Clearly tagged in the UI and API |
| Customer code / containers, payments, payouts, crypto | Not built | Deliberately: see architecture §7–8 |

Full evidence table: [docs/TEST_EVIDENCE.md](docs/TEST_EVIDENCE.md).

## Configuration

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `SHAREDCOMPUTE_DB` | `data/sharedcompute.sqlite3` | Database path |
| `SHAREDCOMPUTE_PLATFORM_FEE_BPS` | `1000` | Marketplace fee in basis points (1000 = 10%) |
| `SHAREDCOMPUTE_SIMULATION_SECONDS` | `8` | How long simulated jobs take |
| `SHAREDCOMPUTE_SEED_SAMPLES` | `1` | Seed the four simulated sample listings |
| `SHAREDCOMPUTE_DEMO_CREDITS` | `1` | Allow simulated top-ups |
| `SHAREDCOMPUTE_ALLOW_SELF_MATCH` | `1` | Let a user's job run on their own device (turn off once money is real) |
| `SHAREDCOMPUTE_MAX_ATTEMPTS` | `3` | Attempts before a job fails |
| `SHAREDCOMPUTE_NODE_TTL_SECONDS` | `30` | Heartbeat freshness window for agent nodes |
| `SHAREDCOMPUTE_CLAIM_TIMEOUT_SECONDS` | `90` | Time for an agent to claim an assigned job |
| `SHAREDCOMPUTE_COOKIE_SECURE` | `0` | Mark session cookies Secure (automatic when exposed) |
| `SHAREDCOMPUTE_REGISTRATION` | `open` locally, `closed` when exposed | Self-registration |
| `SHAREDCOMPUTE_PUBLIC_MARKETPLACE` | `1` locally, `0` when exposed | Browse listings without signing in |
| `SHAREDCOMPUTE_API_DOCS` | `1` locally, `0` when exposed | Serve `/api/docs` |
| `SHAREDCOMPUTE_TRUSTED_PROXIES` | `127.0.0.1` | Proxy addresses trusted with `--behind-tls-proxy` |

Agent: `SHAREDCOMPUTE_SERVER`, `SHAREDCOMPUTE_NODE_TOKEN`, `SHAREDCOMPUTE_CA_FILE`, `SHAREDCOMPUTE_GPUS` (e.g. `0,1`),
`SHAREDCOMPUTE_OFFER_CPU=1`.

## Testing

```bash
. .venv/bin/activate
pytest                                   # 213 tests: marketplace + agent
pip install "llvmlite==0.43.0"           # optional: enables the PTX reproducibility test
python agent/kernels/build_ptx.py --check
sh scripts/check-repo-hygiene.sh         # no secrets, databases or local files tracked or in history
```

The agent's own tests (`agent/tests/`) need no server dependencies and run on Python 3.8. CI
(`.github/workflows/ci.yml`) runs the suite on Linux and macOS (Python 3.10 and 3.12), runs the agent alone on
3.8, checks the PTX is reproducible, and runs the hygiene check.

## Publishing to a private GitHub repository

Not done yet; it needs your approval. When ready:

```bash
sh scripts/check-repo-hygiene.sh                       # must print "Repository hygiene: OK"
gh repo create <owner>/shared-compute --private --source . --remote origin
git push -u origin main feature/mvp-integration        # pushes history; nothing is deployed
```

Then protect `main` (require CI and review) and keep merging through pull requests.

## Security notes

- Never commit `data/`, `.env`, node tokens, TLS keys (`*.key`) or wallets; `.gitignore` and the hygiene script cover them.
- The server binds to loopback by default and refuses public binds without TLS.
- The agent never runs customer code; it only runs fixed, verifiable templates.

## Next milestones

1. Run `diagnose --selftest` and a marketplace GPU job on the first older NVIDIA card; record results.
2. Two-host pilot across separate networks (Tailscale first), including a host dropping out mid-queue.
3. First real workload template in a gVisor/Kata sandbox, with object storage for inputs and outputs.
4. PostgreSQL, email verification, audit log, operator console; closed beta on funded test credits.
5. Payment processor deposits, provider KYC and payouts, refunds, terms, security review.
