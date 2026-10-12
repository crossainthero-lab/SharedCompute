# Test runbook: from one MacBook to a real GPU on another network

Three tests, each harder than the last. Only mark a test **PASSED** when it actually ran on the hardware and
network described, and the evidence is saved. Results so far:

| Test | Status | Evidence |
|---|---|---|
| 1. MacBook only | **PASSED** on 2026-10-12 (Intel MacBook Pro, macOS 26.6, Python 3.12) | `scripts/smoke_local.py` run; see [TEST_EVIDENCE.md](TEST_EVIDENCE.md) |
| 2. Real NVIDIA GPU | **NOT RUN**: no NVIDIA hardware available yet | — |
| 3. Two independent networks | **NOT RUN** | — |

Save evidence under `docs/hardware-results/` (no tokens, passwords or private hostnames) and update
[TEST_EVIDENCE.md](TEST_EVIDENCE.md).

---

## Test 1: MacBook only (CPU and simulated)

**Proves:** the marketplace, real agent process, scheduler, verification and ledger work together on one
machine. It does **not** exercise a GPU.

**Prerequisites:** a development setup (`scripts/bootstrap-dev.sh` completed). No GPU needed.

**Automated (preferred):**
```bash
.venv/bin/python scripts/smoke_local.py
```
This starts a throwaway marketplace on a random loopback port with a temporary database (your `data/`, `.env`
and agent config are not touched). It registers a provider and a customer, adds demo credits, issues a node
token, starts the real agent with `--offer-cpu`, submits a CPU job, then checks the result, evidence and
ledger.

**Expected:** every line `[PASS]`, ending `PASS · evidence: /tmp/…json`, including:
- `CPU job #1 completed with cpu_verified`;
- `customer balance reconciles`, `provider earnings reconcile`, `platform ledger balanced`;
- the agent offered only the CPU, and both Mac GPUs are listed as not offered.

**Manual (to see it in the UI):**
1. `./start.sh` and open http://127.0.0.1:8765. Register, add demo credits under *Economics*, and run
   `.venv/bin/python -m sharedcompute make-operator <email>`.
2. *Provide → Add hardware → Connect a machine* → copy the token →
   `umask 077; printf '%s' 'scn_…' > /tmp/sc-node.token`.
3. `.venv/bin/python -m sharedcompute_agent run --token-file /tmp/sc-node.token --offer-cpu`
4. *Jobs* → **Pipeline check (CPU)**, no VRAM minimum → submit. Also submit a simulated job (e.g. Image generation).
5. Open each job. The CPU job shows **CPU · VERIFIED** with server-measured billing; the simulated one shows **SIMULATED**.
6. *Economics → Platform*: the ledger shows **Balanced**, and revenue equals the sum of the job fees.

**Pass:** the smoke test prints `PASS` (or every manual check holds).
**Fail:** any `[FAIL]`, a job stuck `pending`/`assigned`, or an unbalanced ledger.
**Troubleshooting:**
- Agent never registers: read `agent.log` in the kept temp dir (`--keep`).
- "Refusing plain HTTP": the server URL isn't loopback.
- A stuck CPU job: check that `--offer-cpu` was given.
**Evidence:** the JSON report path printed at the end; the `.venv/bin/python -m pytest` summary.

---

## Test 2: Real low-end NVIDIA GPU (one Linux machine)

**Proves:** the PTX kernel JIT-compiles and runs on a real GPU. It also proves the GPU result is exact,
detection is correct (model, compute capability, driver, CUDA version, display flag, free VRAM), and a
marketplace job completes as `gpu_attested` with plausible timing.

**Prerequisites:**
- Linux, NVIDIA proprietary driver, compute capability ≥ 3.0, Python 3.8+ (3.10+ for the on-box marketplace).
- The owner has chosen which GPU to use, preferably one not driving a display.

**Commands (on the GPU machine):**
```bash
nvidia-smi --query-gpu=index,name,driver_version,memory.total,display_active --format=csv
scripts/bootstrap-agent.sh                                     # installs the agent; offers nothing
AG=~/.local/share/sharedcompute/agent-venv/bin/python
$AG -m sharedcompute_agent diagnose                            # 2a: detection
$AG -m sharedcompute_agent diagnose --gpu <N> --selftest       # 2b: real kernel vs CPU
$AG -m sharedcompute_agent diagnose --gpu <N> --selftest --json > selftest.json
scripts/bootstrap-dev.sh --skip-tests                          # 2c needs the marketplace locally
.venv/bin/python scripts/smoke_local.py --gpu <N> --report smoke-gpu.json   # 2c: full job, same machine
```

**Expected:**
- **2a:** the GPU is listed with the right model, VRAM, `CC x.y`, driver, `CUDA usable`, `can run: agent.cuda_matmul`.
  `drives a display` must match reality.
- **2b:** `Self-test GPU N: PASS · 64x64 int32 matmul matches the CPU exactly · kernel … ms`.
- **2c:** `GPU job #2 completed with gpu_attested`, with `device={'type': 'gpu', 'backend': 'cuda-driver-ptx', …}`.
  `kernel_ms` should be well under the server-observed seconds.

**Pass:** 2a is correct, 2b PASS, and 2c PASS. **Fail:** anything else; keep all output.

**Troubleshooting:** see the table in [HARDWARE_TESTING.md §5](HARDWARE_TESTING.md).
- `libcuda not found`: the driver is missing.
- `INVALID_PTX` / `UNSUPPORTED_PTX_VERSION`: the driver cannot JIT our PTX. Send the JIT log; this is the most
  valuable failure to capture.
- Wrong `display_active`: report it; the display guard relies on it.
- `only N MB of VRAM free`: close other GPU programs.

**Evidence:** the `nvidia-smi` output, `selftest.json`, `smoke-gpu.json`, and a filled-in results template in
`docs/hardware-results/`.

**Extra checks (recommended):**
- Submit a job with "Min. compute capability" above the card's: it should stay pending with an explanation.
- Fill the GPU's VRAM with another program and submit: the job should be **declined** and requeued with
  attempts unchanged.

---

## Test 3: Two independent networks

**Proves:** a provider on one network serves a marketplace on another, over authenticated, encrypted
connections, and the real GPU job is verified and accounted for.

**Prerequisites:**
- Test 2 passed on the GPU machine.
- The marketplace machine (e.g. the Mac) and the GPU machine are on **different networks** (e.g. two homes,
  or one on a phone hotspot).
- Both have Tailscale installed and are logged into the same tailnet, with HTTPS certificates enabled in the
  Tailscale admin console. Any alternative from [REMOTE_CONNECTIVITY.md](REMOTE_CONNECTIVITY.md) also works.
  Do not use public tunnels without approval.

**Commands, marketplace machine:**
```bash
.venv/bin/python -m sharedcompute add-user you@example.com --name William --operator
.venv/bin/python -m sharedcompute serve --behind-tls-proxy     # binds 127.0.0.1:8765
tailscale serve --bg 8765        # https://<mac>.<tailnet>.ts.net (check `tailscale serve --help` for your version)
```
Open `https://<mac>.<tailnet>.ts.net`, sign in, add demo credits, and create an agent node token.

**Commands, GPU machine:**
```bash
umask 077; printf '%s' 'scn_…' > ~/.config/sharedcompute/node.token
# ~/.config/sharedcompute/agent.env:
#   SHAREDCOMPUTE_SERVER=https://<mac>.<tailnet>.ts.net
#   SHAREDCOMPUTE_GPUS=<N>
~/.local/share/sharedcompute/agent-venv/bin/sharedcompute-agent run
```
Then, in the UI, submit a **CUDA integer matmul (GPU)** job and a **Pipeline check (CPU)** job (if the CPU is offered).

**Expected:**
- The agent log shows `Registered as node …`, `Claimed job …`, and `ran on GPU: completed`.
- The job shows **GPU · VERIFIED RESULT**, with the device identity matching and timing consistent.
- The ledger is balanced and provider earnings equal the job's provider share.
- `curl http://<mac-lan-or-public-ip>:8765` from outside the tailnet fails (nothing is publicly exposed).

**Failure-path checks:**
- Stop the agent mid-queue: the job waits, then runs after reconnect.
- Rotate the token in the UI: the old agent gets 401 and stops.
- Start the agent with an `http://` server URL: it is refused.

**Pass:** the GPU job completes `gpu_attested` across the two networks, the ledger reconciles, and the
failure-path checks behave as expected.
**Troubleshooting:**
- TLS errors: the hostname must match the `ts.net` name.
- 401: the token is wrong or rotated.
- The agent is offline in the UI: check `tailscale status` on both machines.
**Evidence:** both agent and server logs (with tokens redacted), screenshots of the job detail and the
economics ledger, and a results file in `docs/hardware-results/`.
