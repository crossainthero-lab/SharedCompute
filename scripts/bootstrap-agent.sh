#!/usr/bin/env bash
# Set up a COMPUTE-PROVIDER machine (the lightweight agent only). Safe to re-run.
#
#   scripts/bootstrap-agent.sh [--nvml] [--selftest-gpu INDEX] [--venv DIR] [--config-dir DIR] [--python PY]
#
# What it does: checks the platform, Python 3.8+ and (read-only) the NVIDIA driver, creates/reuses an
# isolated venv (default ~/.local/share/sharedcompute/agent-venv), installs ./agent, creates
# ~/.config/sharedcompute/agent.env from the template if missing (mode 600, offering NOTHING), runs
# hardware diagnostics and, only if you name a GPU with --selftest-gpu, the real CUDA self-test.
#
# What it never does: use sudo, install or change drivers, install CUDA/PyTorch, start the agent,
# offer any GPU or CPU, write a node token, or open network ports.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)

VENV="${SC_AGENT_VENV:-$HOME/.local/share/sharedcompute/agent-venv}"
CONFIG_DIR="${SC_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/sharedcompute}"
NVML=0
SELFTEST_GPU=""
PY_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --nvml) NVML=1 ;;
    --selftest-gpu) SELFTEST_GPU="$2"; shift ;;
    --venv) VENV="$2"; shift ;;
    --config-dir) CONFIG_DIR="$2"; shift ;;
    --python) PY_OVERRIDE="$2"; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

step "Platform"
OS=$(uname -s)
case "$OS" in
  Linux)
    DISTRO="unknown Linux"
    if [ -r /etc/os-release ]; then . /etc/os-release; DISTRO="${PRETTY_NAME:-$NAME}"; fi
    info "$DISTRO on $(uname -m), kernel $(uname -r)" ;;
  Darwin)
    info "macOS $(sw_vers -productVersion 2>/dev/null || echo '?'): no CUDA on macOS. The agent can still offer the CPU"
    info "(--offer-cpu) for development; GPU workloads will be refused with a reason." ;;
  *) die "Unsupported OS '$OS'. NVIDIA providers need Linux (Windows: untested; use WSL2 only for CPU work). See docs/SETUP_NEW_MACHINE.md." ;;
esac

step "NVIDIA driver (read-only checks)"
NVIDIA_OK=0
if [ "$OS" = Linux ]; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    if nvidia-smi --query-gpu=index,name,driver_version,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/    GPU /'; then
      NVIDIA_OK=1
      info "$(nvidia-smi 2>/dev/null | grep -o 'CUDA Version: [0-9.]*' || echo 'CUDA version: not shown by nvidia-smi')"
    else
      warn "nvidia-smi is installed but failed: the driver may not be loaded (reboot after installing it?)."
    fi
  else
    warn "nvidia-smi not found: no NVIDIA proprietary driver. GPU workloads need it; CPU workloads do not."
  fi
  if lsmod 2>/dev/null | grep -q '^nouveau'; then
    warn "The open-source 'nouveau' driver is loaded. It has no CUDA. Install NVIDIA's driver yourself if you want GPU work"
    warn "(e.g. 'sudo ubuntu-drivers install', or nvidia-driver-470 for Kepler). This script will not change drivers."
  fi
  if ! (ldconfig -p 2>/dev/null | grep -q 'libcuda.so.1'); then
    [ "$NVIDIA_OK" -eq 1 ] && warn "libcuda.so.1 not found in the linker cache; CUDA may not be usable (diagnose will confirm)."
  fi
else
  info "Skipped (not Linux)."
fi

step "Python 3.8+"
py_ok() { "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; }
PY=""
if [ -n "$PY_OVERRIDE" ]; then
  py_ok "$PY_OVERRIDE" || die "$PY_OVERRIDE is not Python 3.8+ with venv support."
  PY="$PY_OVERRIDE"
else
  for cand in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
    if command -v "$cand" >/dev/null 2>&1 && py_ok "$(command -v "$cand")"; then PY=$(command -v "$cand"); break; fi
  done
fi
[ -n "$PY" ] || die "Python 3.8+ with venv not found. Ubuntu/Debian: sudo apt install python3 python3-venv ; then re-run."
info "Using $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

step "Agent environment ($VENV)"
if [ -e "$VENV" ] && ! { [ -x "$VENV/bin/python" ] && py_ok "$VENV/bin/python"; }; then
  aside="$VENV.broken-$(date +%Y%m%d-%H%M%S)"
  mv "$VENV" "$aside"
  info "Existing venv was unusable; moved to $aside"
fi
if [ ! -x "$VENV/bin/python" ]; then
  mkdir -p "$(dirname "$VENV")"
  "$PY" -m venv "$VENV"
  info "Created $VENV"
else
  info "Reusing $VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
if [ "$NVML" -eq 1 ]; then
  "$VENV/bin/python" -m pip install --quiet -e "$REPO/agent[nvml]"
else
  "$VENV/bin/python" -m pip install --quiet -e "$REPO/agent"
fi
info "Installed sharedcompute-agent $("$VENV/bin/python" -c 'import sharedcompute_agent as a; print(a.__version__)') (no CUDA toolkit or PyTorch needed)"

step "Agent configuration ($CONFIG_DIR)"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/agent.env" ]; then
  sed "s#^SHAREDCOMPUTE_TOKEN_FILE=.*#SHAREDCOMPUTE_TOKEN_FILE=$CONFIG_DIR/node.token#" "$REPO/agent/agent.env.example" > "$CONFIG_DIR/agent.env"
  chmod 600 "$CONFIG_DIR/agent.env"
  info "Created agent.env (offers nothing until you set SHAREDCOMPUTE_GPUS / SHAREDCOMPUTE_OFFER_CPU)."
else
  info "agent.env already exists; left unchanged."
fi
if [ -f "$CONFIG_DIR/node.token" ]; then
  perms=$(ls -l "$CONFIG_DIR/node.token" | cut -c1-10)
  case "$perms" in -rw-------) info "Node token present (permissions OK)." ;; *) warn "node.token permissions are $perms; run: chmod 600 $CONFIG_DIR/node.token" ;; esac
else
  info "No node token yet (see next steps)."
fi

step "Hardware diagnostics (read-only)"
SHAREDCOMPUTE_AGENT_ENV="$CONFIG_DIR/agent.env" "$VENV/bin/python" -m sharedcompute_agent diagnose | sed 's/^/    /'
if [ -n "$SELFTEST_GPU" ]; then
  step "CUDA self-test on GPU $SELFTEST_GPU (you chose this GPU)"
  if SHAREDCOMPUTE_AGENT_ENV="$CONFIG_DIR/agent.env" "$VENV/bin/python" -m sharedcompute_agent diagnose --gpu "$SELFTEST_GPU" --selftest | sed 's/^/    /'; then :; fi
  SHAREDCOMPUTE_AGENT_ENV="$CONFIG_DIR/agent.env" "$VENV/bin/python" -m sharedcompute_agent diagnose --gpu "$SELFTEST_GPU" --selftest --json > "$CONFIG_DIR/selftest-gpu$SELFTEST_GPU.json" 2>&1 || true
  info "JSON evidence: $CONFIG_DIR/selftest-gpu$SELFTEST_GPU.json"
fi

step "Done. Next steps (nothing is offered yet)"
cat <<EOF
    1. Diagnose / self-test a GPU you choose:
         $VENV/bin/python -m sharedcompute_agent diagnose --gpu 0 --selftest
    2. In the marketplace UI: Provide -> Add hardware -> Connect a machine. Save the token:
         umask 077; printf '%s' 'scn_...' > $CONFIG_DIR/node.token
    3. Edit $CONFIG_DIR/agent.env:
         SHAREDCOMPUTE_SERVER=https://<marketplace>   (http:// only for this same machine)
         SHAREDCOMPUTE_CA_FILE=<ca.crt>               (private CA only)
         SHAREDCOMPUTE_GPUS=0                         (your explicit choice; display GPUs also need ALLOW_DISPLAY_GPU=1)
    4. Run the agent (foreground; Ctrl-C to stop):
         $VENV/bin/sharedcompute-agent run
    Guides: docs/TEST_RUNBOOK.md (Tests 2 and 3), docs/HARDWARE_TESTING.md, docs/REMOTE_CONNECTIVITY.md
EOF
