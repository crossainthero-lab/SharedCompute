#!/usr/bin/env bash
# Set up a SharedCompute DEVELOPMENT machine (macOS or Linux). Safe to re-run.
#
#   scripts/bootstrap-dev.sh [--skip-tests] [--smoke] [--python /path/to/python3.x]
#
# What it does: detects the platform, finds Python 3.10+, creates/reuses .venv in the repository,
# installs the marketplace and agent in editable mode, creates .env from .env.example if missing,
# creates or upgrades the local SQLite database (backing it up first), runs the tests, and prints next steps.
#
# What it never does: use sudo, install system packages, modify system Python, delete databases,
# change drivers, open ports beyond 127.0.0.1, or offer any GPU/CPU to a marketplace.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)

SKIP_TESTS=0
SMOKE=0
PY_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-tests) SKIP_TESTS=1 ;;
    --smoke) SMOKE=1 ;;
    --python) PY_OVERRIDE="$2"; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

step "Platform"
OS=$(uname -s)
ARCH=$(uname -m)
case "$OS" in
  Darwin) info "macOS $(sw_vers -productVersion 2>/dev/null || echo '?') on $ARCH" ;;
  Linux)
    DISTRO="unknown Linux"
    if [ -r /etc/os-release ]; then . /etc/os-release; DISTRO="${PRETTY_NAME:-$NAME}"; fi
    info "$DISTRO on $ARCH" ;;
  MINGW*|MSYS*|CYGWIN*)
    die "Windows is not supported by this script. Use WSL2 (Ubuntu) and run it there, or follow the manual steps in docs/SETUP_NEW_MACHINE.md." ;;
  *) die "Unsupported OS '$OS'. Follow the manual steps in docs/SETUP_NEW_MACHINE.md." ;;
esac
info "Repository: $REPO (branch $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?'))"

step "Python 3.10+"
py_ok() { "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }
PY=""
if [ -n "$PY_OVERRIDE" ]; then
  py_ok "$PY_OVERRIDE" || die "$PY_OVERRIDE is not Python 3.10+ with venv support."
  PY="$PY_OVERRIDE"
elif [ -x .venv/bin/python ] && py_ok .venv/bin/python; then
  PY=.venv/bin/python
else
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1 && py_ok "$(command -v "$cand")"; then PY=$(command -v "$cand"); break; fi
  done
fi
if [ -z "$PY" ] && command -v uv >/dev/null 2>&1; then
  info "No suitable system Python; asking uv for a user-level Python 3.12 (no system changes)."
  PY=$(uv python find 3.12 2>/dev/null || (uv python install 3.12 >/dev/null && uv python find 3.12)) || PY=""
fi
if [ -z "$PY" ]; then
  if [ "$OS" = Darwin ]; then
    die "Python 3.10+ not found (macOS's /usr/bin/python3 is usually 3.9). Install one without touching system Python:
       brew install python@3.12        # Homebrew
   or  curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12
   then re-run this script."
  else
    die "Python 3.10+ with venv not found. On Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip
   (Ubuntu 20.04: sudo apt install python3.10 python3.10-venv via the deadsnakes PPA, or use uv), then re-run."
  fi
fi
info "Using $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

step "Virtual environment (.venv)"
if [ -e .venv ] && ! { [ -x .venv/bin/python ] && py_ok .venv/bin/python; }; then
  aside=".venv.broken-$(date +%Y%m%d-%H%M%S)"
  mv .venv "$aside"
  info "Existing .venv was unusable; moved it to $aside (delete it when you're happy)."
fi
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
  info "Created .venv"
else
  info "Reusing .venv ($(.venv/bin/python -c 'import sys; print(sys.version.split()[0])'))"
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e '.[dev]'
.venv/bin/python -c 'import sharedcompute, sharedcompute_agent; print("    sharedcompute", sharedcompute.__version__, "· agent", sharedcompute_agent.__version__)'

step "Local configuration"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  info "Created .env from .env.example (git-ignored; edit for this machine)."
else
  info ".env already exists; left unchanged."
fi
mkdir -p data
.venv/bin/python -m sharedcompute migrate | sed 's/^/    /'

step "Hardware (read-only; nothing is offered)"
.venv/bin/python -m sharedcompute_agent diagnose | sed 's/^/    /'

if [ "$SKIP_TESTS" -eq 0 ]; then
  step "Automated tests"
  .venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -3 | sed 's/^/    /'
  .venv/bin/python -m pytest -q -p no:cacheprovider >/dev/null 2>&1 || die "Tests failed. Run: .venv/bin/python -m pytest"
fi
if [ "$SMOKE" -eq 1 ]; then
  step "End-to-end smoke test (temporary marketplace + real agent, CPU job)"
  .venv/bin/python scripts/smoke_local.py | sed 's/^/    /'
fi

step "Done. Next steps"
cat <<EOF
    Start the marketplace:      ./start.sh                     -> http://127.0.0.1:8765
    Run the tests:              .venv/bin/python -m pytest
    End-to-end smoke test:      .venv/bin/python scripts/smoke_local.py
    Health check (read-only):   scripts/doctor.sh
    Make yourself an operator:  .venv/bin/python -m sharedcompute make-operator you@example.com   (after registering)
    Offer this machine's CPU:   see docs/TEST_RUNBOOK.md (Test 1)
EOF
