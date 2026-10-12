# Hardware testing: connecting the first older NVIDIA GPU

This guide takes one Linux machine with an older NVIDIA card from "nothing installed" to "a verified
GPU job settled in the marketplace". Nothing here needs a CUDA toolkit, PyTorch, Docker or cloud services.

> **Status on 2026-10-12:** none of these steps has yet run on NVIDIA hardware. Everything up to the GPU has
> been tested (see [TEST_EVIDENCE.md](TEST_EVIDENCE.md)). The first run on a real card is the test this
> guide exists for. Please record the results with the template at the end.

## 1. Will my GPU work?

The GPU workload (`agent.cuda_matmul`) ships as PTX for `sm_30` with PTX ISA 3.2. The NVIDIA driver compiles
it for the installed card when it loads.

| Requirement | Minimum | Examples |
|---|---|---|
| Architecture (compute capability) | Kepler, CC 3.0 | GTX 650–780 (CC 3.0/3.5), GT 710/730 GK208 (3.5), GTX 750/750 Ti (5.0), GTX 950–980 (5.2), GTX 1050–1080 (6.1), GTX 16xx / RTX 20xx (7.5) |
| NVIDIA proprietary driver | supports CUDA 5.5 | Any driver for the cards above. Kepler cards need the 470.xx legacy branch. Check NVIDIA's support list for your card. |
| Free VRAM | 64 MB | 1 GB cards are fine |
| OS / Python | Linux x86_64, Python 3.8+ | Ubuntu 20.04+, Debian 11+ |

**Not supported:** Fermi (GTX 400/500 series, and Fermi-based GT 610/620/630, CC 2.x). The open-source
`nouveau` driver has no CUDA. AMD and Intel GPUs have no CUDA either. The server and the agent both refuse
these with a reason; no job is sent to them.

Windows has untested code paths for `nvcuda.dll`. Use Linux for the first test.

## 2. Prepare the GPU machine

```bash
nvidia-smi                       # must list the card; the header shows "CUDA Version: X.Y"
lsmod | grep -E '^nvidia|nouveau'   # expect nvidia, not nouveau
python3 --version                # 3.8 or newer
```

If `nvidia-smi` is missing, install the proprietary driver (Ubuntu: `sudo ubuntu-drivers install`; Kepler:
`sudo apt install nvidia-driver-470`) and reboot.

## 3. Install the agent only

Shortcut: `scripts/bootstrap-agent.sh` does this and step 4 safely (isolated venv, config that offers nothing,
read-only diagnostics). The manual equivalent:

The agent has no third-party dependencies. Copy this repository (or just the `agent/` folder) to the machine:

```bash
python3 -m venv ~/sc-agent && . ~/sc-agent/bin/activate
pip install ./agent                    # optional: pip install './agent[nvml]' for NVML metrics
```

## 4. Diagnose (no network, nothing offered)

```bash
python -m sharedcompute_agent diagnose
```

Expected shape:

```
SharedCompute agent 0.3.0 on Linux 5.15.0 x86_64 (Python 3.10.12)
CUDA driver: API 12020 (CUDA 12.2), 1 device(s)

GPU 0  NVIDIA GeForce GTX 1050 Ti · 4096 MB VRAM (3900 MB free) · CC 6.1 · driver 535.183.01 · CUDA usable · via nvidia-smi
       can run: agent.cuda_matmul
       offered with these flags: no — Not offered. Start the agent with --gpu 0 to offer it.
CPU    Intel(R) Core(TM) i5-4590 CPU @ 3.30GHz (4 threads)
       can run: agent.hashchain
       offered with these flags: no — Not offered. Start the agent with --offer-cpu to run CPU workloads.
```

**Your main GPU is safe by default.** Nothing is offered unless you name it with `--gpu`. A GPU that drives a
monitor is still refused unless you also pass `--allow-display-gpu`. For the first test, use a GPU with no
monitor attached, or connect over SSH with the desktop idle.

## 5. Self-test the real kernel locally

```bash
python -m sharedcompute_agent diagnose --gpu 0 --selftest
```

This loads the PTX through the driver, runs a 64×64 integer matrix multiply on GPU 0, and compares every
element with the CPU result. A pass looks like
`Self-test GPU 0: PASS · 64x64 int32 matmul matches the CPU exactly · kernel 0.0123 ms`.

| Failure text | Meaning | What to do |
|---|---|---|
| `libcuda not found` | Driver not installed, or running in a container without GPU access | Install the proprietary driver; outside containers |
| `CUDA_ERROR_NO_DEVICE` / `CUDA_ERROR_UNKNOWN` at cuInit | Driver/kernel module mismatch, or nouveau loaded | Reboot after driver install; check `lsmod` |
| `CUDA_ERROR_INVALID_PTX` / `UNSUPPORTED_PTX_VERSION` (with JIT log) | The driver could not compile the PTX | Send the full output; this is the most important bug to catch |
| `only N MB of VRAM free` | Another program is using the GPU | Close it, or pick another GPU |
| `N elements differ from the CPU result` | Wrong results from the GPU | Send the output; do not offer this GPU |

Keep `diagnose --gpu 0 --selftest --json` output for the results record.

## 6. Run a marketplace the agent can reach

Pick one option from [REMOTE_CONNECTIVITY.md](REMOTE_CONNECTIVITY.md). For a first test where both machines
are on **one home network**, the quickest is a private CA on the Mac:

```bash
# On the Mac (marketplace host); 192.168.1.20 = the Mac's LAN address
scripts/make-dev-tls.sh ~/sc-tls mac.lan 192.168.1.20
python -m sharedcompute add-user you@example.com --name "William" --operator
python -m sharedcompute serve --host 0.0.0.0 --tls-cert ~/sc-tls/server.crt --tls-key ~/sc-tls/server.key
```

Exposed servers close registration (hence `add-user`) and require sign-in for the marketplace. Copy
`~/sc-tls/ca.crt` to the GPU machine; it is not secret. **Never copy `ca.key`.**

For **separate networks** (the real goal), use Tailscale or a small VPS as described in REMOTE_CONNECTIVITY.md.
The agent always connects outbound, so the GPU machine needs no open ports.

## 7. Connect the agent

1. In the web UI (`https://192.168.1.20:8765`, trusting `ca.crt` in your browser or accepting the warning
   once), go to **Provide → Add hardware → Connect a machine**. Copy the token; it is shown once.
2. On the GPU machine:

```bash
umask 077; printf '%s' 'scn_…' > ~/.sharedcompute-node-token
python -m sharedcompute_agent run --server https://192.168.1.20:8765 --ca-file ~/ca.crt \
  --token-file ~/.sharedcompute-node-token --gpu 0
```

Expected log: `Registered as node N (...). Offered: NVIDIA GeForce GTX 1050 Ti [agent.cuda_matmul]`.
The Provide page shows the GPU with its CC, driver, CUDA version and free VRAM.

## 8. Run a GPU job

On **Jobs**, choose **CUDA integer matmul (GPU)** with 2 estimated minutes and any small budget, then submit.
The quote names your GPU. Within a few seconds the job should complete. Open it and check:

- the badge reads **GPU · VERIFIED RESULT**;
- the checklist shows the result verified, GPU execution reported (`cuda-driver-ptx`), the device identity
  matching, and timing consistent;
- billing uses the server-measured time.

Then try the failure paths:

| Test | How | Expected |
|---|---|---|
| Customer CC too high | Submit with "Min. compute capability" above your card's | Quote: "No compatible GPU has compute capability X or newer"; job stays pending |
| Agent restart mid-queue | Stop the agent (Ctrl-C), submit, start it again | Job waits, then runs after reconnect |
| Agent killed mid-job | Not practical (jobs take milliseconds) | Covered by automated tests |
| VRAM pressure | Run another GPU program that fills VRAM, then submit | Job is **declined** ("only N MB of VRAM free"), requeued, attempts unchanged |
| CPU fallback | Only happens if CUDA fails *after* the GPU was offered and the agent runs with `--allow-cpu-fallback`; not reproducible on demand on healthy hardware | Covered by automated tests: job fails as **CPU FALLBACK**, not billed |

## 9. Record the results

Copy this into `docs/hardware-results/YYYY-MM-DD-<gpu>.md`, without tokens or hostnames you consider private:

```markdown
# <GPU model> on <date>
- Tester:
- Machine: CPU / RAM / OS + kernel / Python
- GPU: model, VRAM, compute capability (from diagnose)
- Driver: nvidia-smi driver version; CUDA version in nvidia-smi header; driver API from diagnose
- Display attached to this GPU: yes/no
- Network setup: same LAN / Tailscale / VPS; TLS method
- diagnose --selftest: PASS/FAIL, kernel ms (attach --json output)
- Marketplace GPU job id: , evidence level: , server-observed seconds: , kernel_ms:
- Failure-path tests (section 8): results
- Anything unexpected:
```
