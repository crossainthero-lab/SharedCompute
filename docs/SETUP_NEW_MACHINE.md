# Setting up SharedCompute on a new machine

This is the procedure an agent follows when told **"set up this SharedCompute repo on this machine."** Work
through it top to bottom. Every step is safe to repeat. Stop and ask the owner whenever a step says so.
The rules in [CLAUDE.md §4](../CLAUDE.md#4-non-negotiable-rules) apply throughout.

## 0. Get the code and identify the machine

```bash
git clone https://github.com/crossainthero-lab/SharedCompute.git   # if access is refused: gh auth login
cd SharedCompute
git checkout feature/mvp-integration                              # the current development branch
uname -s; uname -m
sw_vers 2>/dev/null || (. /etc/os-release && echo "$PRETTY_NAME")
command -v nvidia-smi && nvidia-smi --query-gpu=index,name,driver_version,display_active,memory.total --format=csv
scripts/doctor.sh                                                 # read-only
```

Pick the role:

| You see | Role |
|---|---|
| macOS, any GPU | **A. Development (macOS)**. CUDA never works on macOS; the agent can offer the CPU only |
| Linux, owner wants to develop or run the marketplace | **B. Development (Linux)** |
| Linux with an NVIDIA GPU, owner wants to provide compute | **C. Compute provider (Linux NVIDIA)**. B and C can be the same machine |
| Windows, `nouveau`, a Fermi GPU (GTX 400/500), AMD/Intel/Apple GPU only, Python < 3.8 | **D. Unsupported or limited**: see §D |

If the request is ambiguous ("set up the repo"), do A or B. Do C only if the owner says this machine
should provide compute. Never infer that a GPU may be used.

## A. Development machine: macOS

Tested on: Intel MacBook Pro (Core i7-9750H, Intel UHD 630 + AMD Radeon Pro 5300M), macOS 26.6.

1. **Python 3.10+, without touching system Python.** macOS's `/usr/bin/python3` is usually 3.9, which is too old.
   The bootstrap script finds `python3.13…python3.10`, or uses `uv` for a user-level Python if `uv` is installed.
   If neither is present, ask the owner to install one:
   - `brew install python@3.12`, or
   - `curl -LsSf https://astral.sh/uv/install.sh | sh` then `uv python install 3.12`.
2. **Bootstrap:**
   ```bash
   scripts/bootstrap-dev.sh            # add --smoke to also run Test 1 automatically
   ```
   It creates `.venv` in the repo, installs the marketplace and agent in editable mode, copies `.env.example` →
   `.env` (if missing), creates or upgrades `data/sharedcompute.sqlite3` (backing up first), runs read-only
   hardware diagnostics, and runs the test suite.
3. **Start and use:**
   ```bash
   ./start.sh                          # http://127.0.0.1:8765, loopback only
   ```
   Register in the browser, then `.venv/bin/python -m sharedcompute make-operator <email>` to see platform economics.
4. **Simulated providers and CPU-only work** need no extra setup. The four sample GPUs (tagged `SIM`) always
   exist. To run real CPU jobs, connect this Mac as a provider using **CPU only** ([TEST_RUNBOOK.md Test 1](TEST_RUNBOOK.md)).
5. **Verify:** `.venv/bin/python scripts/smoke_local.py` must print `PASS`.

Expected hardware report on the reference Mac: both GPUs "cannot run agent.cuda_matmul: not an NVIDIA GPU",
and the CPU "can run agent.hashchain". That is correct, not a fault.

## B. Development machine: Linux (Ubuntu and compatible)

1. Identify the distribution: `. /etc/os-release; echo "$PRETTY_NAME"`.
2. Python 3.10+ with `venv`. Check with `python3 --version` and `python3 -c 'import venv, ensurepip'`. If missing,
   **ask the owner** before running (needs sudo):
   - Ubuntu 22.04+ / Debian 12+: `sudo apt install python3 python3-venv python3-pip`
   - Ubuntu 20.04 (Python 3.8): install `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`), then re-run
     the bootstrap; it will use a user-level Python 3.12. The deadsnakes PPA (`python3.12-venv`) also works.
   - Fedora: `sudo dnf install python3`; Arch: `sudo pacman -S python`.
3. `scripts/bootstrap-dev.sh`, then `./start.sh` and `.venv/bin/python scripts/smoke_local.py` as for macOS.
4. Headless machine: the marketplace still binds `127.0.0.1`. Use an SSH tunnel
   (`ssh -L 8765:127.0.0.1:8765 host`) to reach the UI. Do not bind `0.0.0.0` without TLS ([REMOTE_CONNECTIVITY.md](REMOTE_CONNECTIVITY.md)).

## C. Compute-provider machine: Linux + NVIDIA

Goal: install only the lightweight agent, prove the GPU works locally, then connect it to a marketplace. The
owner decides which GPU, if any, is offered.

1. **Read-only hardware check:**
   ```bash
   nvidia-smi
   nvidia-smi --query-gpu=index,name,driver_version,memory.total,display_active,compute_cap --format=csv
   lsmod | grep -E '^(nvidia|nouveau)'
   ```
   Requirements: proprietary NVIDIA driver (not nouveau), compute capability ≥ 3.0 (Kepler or newer), a driver
   supporting CUDA 5.5+, and 64 MB free VRAM. Old drivers may reject the `compute_cap` field; the agent copes.
   If the driver is missing or wrong, **stop and tell the owner what to install** (`sudo ubuntu-drivers install`,
   or `nvidia-driver-470` for Kepler). Never install or change drivers yourself.
2. **Install the agent** (Python 3.8+, no CUDA toolkit, no PyTorch):
   ```bash
   scripts/bootstrap-agent.sh                 # add --nvml for NVML metrics (small pip package)
   ```
   This creates `~/.local/share/sharedcompute/agent-venv` and `~/.config/sharedcompute/agent.env` (mode 600,
   offering nothing), then runs `diagnose`. It does not start the agent.
3. **Ask the owner which GPU to test.** Show them the `diagnose` output, especially which GPU has
   `drives a display`. Never choose for them. Then run the real CUDA self-test on their choice:
   ```bash
   ~/.local/share/sharedcompute/agent-venv/bin/python -m sharedcompute_agent diagnose --gpu <N> --selftest
   ```
   `PASS` means the driver JIT-compiled the PTX and the GPU's result matched the CPU exactly. On `FAIL`, keep
   the full output and use the troubleshooting table in [HARDWARE_TESTING.md](HARDWARE_TESTING.md).
4. **Optional local end-to-end test** (marketplace and agent on this machine). Needs the dev setup (B):
   `.venv/bin/python scripts/smoke_local.py --gpu <N>`, which should end with `PASS` and a `gpu_attested` job.
5. **Connect to a marketplace:**
   - Get a node token from the marketplace UI (*Provide → Add hardware → Connect a machine*) and save it with
     `umask 077; printf '%s' 'scn_…' > ~/.config/sharedcompute/node.token`.
   - Edit `~/.config/sharedcompute/agent.env`:
     - `SHAREDCOMPUTE_SERVER=https://…` (`http://` only if the marketplace is on this same machine);
     - `SHAREDCOMPUTE_CA_FILE=…` (private CA only);
     - `SHAREDCOMPUTE_GPUS=<N>` (the owner's choice).
   - Run `~/.local/share/sharedcompute/agent-venv/bin/sharedcompute-agent run` (foreground; Ctrl-C stops it).
   - For other networks use Tailscale (recommended) or a VPS: [REMOTE_CONNECTIVITY.md](REMOTE_CONNECTIVITY.md).
     Never create public tunnels or open ports without approval.
6. **Record the results** in `docs/hardware-results/YYYY-MM-DD-<gpu>.md` (template in HARDWARE_TESTING.md §9)
   and update `docs/TEST_EVIDENCE.md`.

## D. Unsupported or limited hardware

Say exactly what works and what doesn't; never claim support that wasn't verified.

| Situation | What works | What doesn't | What to tell the owner |
|---|---|---|---|
| macOS (any) | Development, marketplace, CPU-only agent | GPU workloads (no CUDA on macOS) | Use a Linux NVIDIA machine for GPU work |
| Windows | Nothing via scripts. WSL2 (Ubuntu) works for development and CPU-only agents | GPU agent on native Windows (code path untested); bootstrap scripts | Use WSL2 for dev; Linux for GPU providers |
| Linux with `nouveau` | CPU-only agent | CUDA | The owner installs the proprietary driver, then re-run C |
| Fermi GPU (GTX 400/500, CC 2.x) | CPU-only agent | `agent.cuda_matmul` (needs CC ≥ 3.0) | Refused with "compute capability … below 3.0" |
| Driver older than CUDA 5.5 | CPU-only agent | GPU workloads | Update the driver (owner's decision) |
| AMD / Intel / Apple GPU | CPU-only agent | CUDA | Not an NVIDIA GPU |
| GPU drives the only display | Everything, if the owner allows it | Unattended use is refused by default | Prefer a second GPU or a headless box; otherwise `--allow-display-gpu` |
| Python < 3.8 | Nothing | Agent | Install Python 3.8+ (owner's decision) |
| Python 3.8–3.9 | Agent | Marketplace (needs 3.10+) | Fine for a provider-only machine |

**Manual setup** (any OS where the scripts don't run):
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'   # dev, Python 3.10+
python -m sharedcompute migrate && python -m sharedcompute serve
python3 -m venv ~/sc-agent && ~/sc-agent/bin/pip install ./agent         # agent only, Python 3.8+
~/sc-agent/bin/python -m sharedcompute_agent diagnose
```

## After setup: report back

Tell the owner, with evidence:
- the role set up;
- the Python and tool versions used;
- the test results (passed, skipped, failed, with counts);
- the hardware detected and what each device can run;
- what was *not* verified;
- the exact next command for them.
Never mark Test 2 or Test 3 as passed without a real GPU / second network.
