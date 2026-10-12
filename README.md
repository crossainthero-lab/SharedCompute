# SharedCompute

A marketplace that matches AI compute jobs to independently owned GPUs. Providers list hardware and set
an hourly price; customers submit jobs with requirements and a budget; a scheduler picks the cheapest
compatible GPU; completed work is settled with a 10% marketplace fee and a 90% provider share.

Founded by William and Stefan. This is **v0.2, a local MVP**. It builds on the v0.1 founders' draft
(see git history and [`PROJECT_BLUEPRINT.md`](PROJECT_BLUEPRINT.md)).

> **Status in one line:** accounts, marketplace, scheduling, queueing, failure handling, the ledger and a
> real provider agent all work. Balances are simulated credits, the four sample listings are simulated,
> and real execution is limited to allowlisted, server-verifiable workloads. No money moves, and no
> customer code runs on provider machines.

## Quick start

Requires Python 3.10+ (tested on 3.12 and 3.14).

```bash
./start.sh                 # first run creates .venv and installs dependencies
```

Open http://127.0.0.1:8765. Data is stored in `data/sharedcompute.sqlite3`, which is git-ignored.

Manual equivalent:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
python -m sharedcompute serve            # --host / --port available
```

To see platform-wide economics, register in the UI, then:

```bash
python -m sharedcompute make-operator you@example.com
```

## Try the whole flow

1. **Browse** the marketplace. Four sample GPUs (tagged `SIM`) are always available.
2. **Create an account**, open *Economics* and add demo credits (simulated; no payment is taken).
3. **Submit a job** on *Jobs*. The live quote shows which GPU would be matched, the price and the fee split,
   or why the job would queue (busy, over budget, no compatible GPU). Your budget is held until the job ends.
4. Watch it move through *assigned → running → completed* (simulated jobs finish after 8 s), or use the demo
   controls to finish it now or inject a failure. Open a job to see its timeline and settlement.
5. **Provide**: add a simulated listing, change its price, pause it (running jobs return to the queue).
6. **Economics**: your balances and ledger; as an operator, volume, revenue, provider earnings, a 14-day
   chart and a ledger integrity check.

### Connect a real machine (provider agent)

1. On *Provide*, choose **Connect a machine**, then create the node. Copy the token; it is shown once.
2. On the GPU machine, from a checkout of this repository:

   ```bash
   python3 -m venv .venv && . .venv/bin/activate && pip install -e .
   python -m sharedcompute_agent detect                  # print what will be reported
   export SHAREDCOMPUTE_NODE_TOKEN='scn_…'
   python -m sharedcompute_agent run --server http://<marketplace-host>:8765
   ```

   For NVIDIA machines, `pip install -e '.[nvml]'` adds NVML detection and live metrics; installing PyTorch
   with CUDA enables the `agent.matmul` benchmark workload.
3. The node shows **Online** with its detected GPUs. Submit a **Pipeline check** job: the agent executes it,
   the server verifies the result, and the job is billed by measured runtime.

To serve agents on other machines, bind with `--host 0.0.0.0`. Do this only on a trusted network, or behind
a TLS-terminating reverse proxy with `SHAREDCOMPUTE_COOKIE_SECURE=1`. The token travels in a header, so plain
HTTP across the internet exposes it.

## Architecture

```
Browser / API client ──▶ FastAPI (/api/v1) ──▶ scheduler + queue ──▶ SQLite (jobs, events, ledger)
                                   ▲                    ▲
          provider agents ── HTTPS (outbound) ──▶ agent API (/api/agent/v1) ──▶ verifiers ──▶ ledger
```

- `sharedcompute/` is the marketplace: `api/` (HTTP layer), `services/` (accounts, nodes, jobs, scheduler,
  agent protocol, ledger, economics), `schema.sql`, `workloads.py` (catalogue + verifiers), `web/` (UI).
- `agent/sharedcompute_agent/` is the provider agent: GPU discovery (`gpu.py`), allowlisted workloads
  (`workloads.py`), the client and the run loop. Standard library only.
- `tests/` has 117 tests, including a real-socket end-to-end test with the real agent loop.

Full design, including the job state machine, agent protocol, trust levels, isolation plan for customer
workloads, ledger entries, security model and scaling path: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.
The API reference is served at http://127.0.0.1:8765/api/docs.

## What works, what is simulated, what is not built

| Area | State | Notes |
|---|---|---|
| Accounts, sessions, ownership checks | Works | PBKDF2, HttpOnly cookie, CSRF header, rate-limited login. No email verification or password reset. |
| Marketplace listings and live availability | Works | Status comes from provider settings, heartbeats and active jobs. |
| Scheduler, queue, budgets, retries | Works | Cheapest compatible GPU within budget; FIFO; up to 3 attempts. |
| Provider pricing and availability controls | Works | Price changes apply to new matches; pausing requeues running work. |
| Double-entry ledger and 10% / 90% split | Works | Holds, charges and releases always balance; tested after every scenario. |
| Sample network (4 GPUs) | **Simulated** | Seeded listings; that hardware does not exist. |
| Image / inference / fine-tune / batch jobs | **Simulated** | Timed by the server; billed for the estimated minutes; nothing executes. |
| Balances and payments | **Simulated** | Demo credits only. No deposits, payouts, invoices or tax. |
| Provider agent: register, heartbeat, claim, report | Works | Verified on a real machine (an Intel Mac with Intel UHD 630 + AMD Radeon Pro 5300M) and over real sockets in tests. |
| GPU discovery | Partial | macOS path verified on real hardware. NVML and nvidia-smi paths are tested only against synthetic output in the documented formats; **not yet run on an NVIDIA machine**. |
| `agent.hashchain` pipeline check | Works | Really executes on the provider (on the CPU) and is verified by recomputation. Proves the path, not GPU compute. |
| `agent.matmul` CUDA benchmark | Partial | Exact verifiable checksum implemented; **never executed on NVIDIA hardware**. |
| Distributed execution across networks | Not yet demonstrated | The real-agent run had agent and server on the same machine. Nothing has yet run across two networks. |
| Customer-supplied code or containers | Not built | Deliberately absent until sandboxing exists (see architecture §7). |
| Crypto settlement, token, wallets | Not built | No token will be issued; stablecoin payouts are a possible later option. |

## Configuration

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `SHAREDCOMPUTE_DB` | `data/sharedcompute.sqlite3` | Database path |
| `SHAREDCOMPUTE_PLATFORM_FEE_BPS` | `1000` | Marketplace fee in basis points (1000 = 10%) |
| `SHAREDCOMPUTE_SIMULATION_SECONDS` | `8` | How long simulated jobs take |
| `SHAREDCOMPUTE_SEED_SAMPLES` | `1` | Seed the four simulated sample listings |
| `SHAREDCOMPUTE_DEMO_CREDITS` | `1` | Allow simulated top-ups |
| `SHAREDCOMPUTE_ALLOW_SELF_MATCH` | `1` | Let a user's job run on their own GPU (turn off once money is real) |
| `SHAREDCOMPUTE_MAX_ATTEMPTS` | `3` | Attempts before a job fails |
| `SHAREDCOMPUTE_NODE_TTL_SECONDS` | `30` | Heartbeat freshness window for agent nodes |
| `SHAREDCOMPUTE_CLAIM_TIMEOUT_SECONDS` | `90` | Time for an agent to claim an assigned job |
| `SHAREDCOMPUTE_COOKIE_SECURE` | `0` | Mark session cookies Secure (set behind HTTPS) |

## Testing

```bash
. .venv/bin/activate
pytest
```

Coverage: money rounding and fee splits; auth, sessions, CSRF and rate limits; listings, validation and
ownership; matching, budgets, region/VRAM constraints, FIFO queueing, the simulator, cancellation in every
state, requeue and max-attempt failure; ledger invariants; the agent protocol (registration, heartbeats,
leases, time limits, verification, metered billing, token rotation, isolation between nodes); GPU discovery
parsers; server/agent workload cross-checks; HTTP error shapes, limits and security headers; the v0.1
draft's scenarios ported to the new API; and an end-to-end run of the real agent over real HTTP.

## Security notes

- Never commit `data/`, `.env` files or node tokens; `.gitignore` covers them.
- The server binds to `127.0.0.1` by default and warns when bound elsewhere. There is no built-in TLS.
- A v0.1 `sharedcompute.sqlite3` is refused at startup rather than migrated (the old demo data has no owners).

## Next milestones

1. Run the agent on a Linux + NVIDIA machine; validate NVML detection and the `agent.matmul` workload.
2. Two-host pilot: two owned GPUs on different home networks, a TLS-fronted marketplace, failover mid-job.
3. First real workload template in a gVisor/Kata sandbox, with object storage for inputs and outputs.
4. PostgreSQL, email verification, audit log, operator console; closed beta on funded test credits.
5. Payment processor deposits, provider KYC and payouts, refunds, terms, security review.

Details in [docs/ARCHITECTURE.md §11](docs/ARCHITECTURE.md#11-milestones-toward-real-gpu-execution).
