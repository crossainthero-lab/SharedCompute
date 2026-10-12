#!/usr/bin/env bash
# Read-only health check for a SharedCompute checkout and this machine. Changes nothing.
#
#   scripts/doctor.sh [--agent-venv DIR] [--config-dir DIR]
#
# Reports PASS / WARN / FAIL for: repository and branch, platform, Python, the dev venv, local config and
# database schema, a running marketplace, the provider agent install and config, hardware, and repo hygiene.
# Exit code 1 if anything FAILs.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)
AGENT_VENV="${SC_AGENT_VENV:-$HOME/.local/share/sharedcompute/agent-venv}"
CONFIG_DIR="${SC_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/sharedcompute}"
while [ $# -gt 0 ]; do
  case "$1" in
    --agent-venv) AGENT_VENV="$2"; shift ;;
    --config-dir) CONFIG_DIR="$2"; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

PASS=0; WARN=0; FAIL=0
pass() { printf '  PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
warn() { printf '  WARN  %s\n' "$*"; WARN=$((WARN + 1)); }
fail() { printf '  FAIL  %s\n' "$*"; FAIL=$((FAIL + 1)); }
note() { printf '        %s\n' "$*"; }
section() { printf '\n%s\n' "$*"; }

section "Repository"
if git rev-parse --git-dir >/dev/null 2>&1; then
  branch=$(git rev-parse --abbrev-ref HEAD)
  pass "git checkout on '$branch' at $(git rev-parse --short HEAD)"
  [ "$branch" = "feature/mvp-integration" ] || warn "Current development happens on 'feature/mvp-integration' (see README)"
  dirty=$(git status --porcelain | wc -l | tr -d ' ')
  [ "$dirty" = 0 ] && pass "working tree clean" || warn "$dirty uncommitted change(s)"
  remote=$(git remote get-url origin 2>/dev/null || echo "none")
  note "origin: $remote"
else
  fail "not a git checkout"
fi

section "Platform"
OS=$(uname -s)
case "$OS" in
  Darwin) pass "macOS $(sw_vers -productVersion 2>/dev/null) on $(uname -m): development machine; CPU-only agent possible, no CUDA" ;;
  Linux) desc=$( (. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-Linux}") ); pass "$desc on $(uname -m)" ;;
  *) warn "$OS is not a supported platform (see docs/SETUP_NEW_MACHINE.md)" ;;
esac

section "Python"
for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    note "$cand -> $(command -v "$cand") ($("$cand" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null))"
  fi
done
if [ -x .venv/bin/python ]; then
  v=$(.venv/bin/python -c 'import sys; print(sys.version.split()[0])')
  if .venv/bin/python -c 'import sharedcompute, sharedcompute_agent, fastapi' >/dev/null 2>&1; then
    pass "dev venv .venv (Python $v) imports sharedcompute $(.venv/bin/python -c 'import sharedcompute as s; print(s.__version__)')"
  else
    fail "dev venv .venv exists but packages are missing: run scripts/bootstrap-dev.sh"
  fi
else
  warn "no dev venv (.venv): run scripts/bootstrap-dev.sh on development machines"
fi

section "Marketplace (local)"
[ -f .env ] && pass ".env present (machine settings, git-ignored)" || warn "no .env (defaults apply; bootstrap-dev.sh creates one)"
if [ -x .venv/bin/python ]; then
  .venv/bin/python -m sharedcompute migrate --check >/tmp/sc-doctor-migrate.$$ 2>&1
  rc=$?
  sed 's/^/        /' /tmp/sc-doctor-migrate.$$; rm -f /tmp/sc-doctor-migrate.$$
  case $rc in
    0) pass "database schema is current" ;;
    1) warn "database missing or behind: run .venv/bin/python -m sharedcompute migrate (backs up first)" ;;
    *) fail "could not read the database" ;;
  esac
fi
if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 2 http://127.0.0.1:8765/api/health >/tmp/sc-doctor-health.$$ 2>/dev/null; then
  pass "marketplace running on http://127.0.0.1:8765 ($(cat /tmp/sc-doctor-health.$$))"
else
  note "no marketplace answering on 127.0.0.1:8765 (start one with ./start.sh)"
fi
rm -f /tmp/sc-doctor-health.$$

section "Provider agent"
AGENT_PY=""
if [ -x "$AGENT_VENV/bin/python" ] && "$AGENT_VENV/bin/python" -c 'import sharedcompute_agent' >/dev/null 2>&1; then
  AGENT_PY="$AGENT_VENV/bin/python"
  pass "agent venv $AGENT_VENV (agent $("$AGENT_PY" -c 'import sharedcompute_agent as a; print(a.__version__)'))"
elif [ -x .venv/bin/python ]; then
  AGENT_PY=.venv/bin/python
  note "no separate agent venv; using the dev venv's agent for diagnostics"
else
  warn "agent not installed (provider machines: scripts/bootstrap-agent.sh)"
fi
cfg="$CONFIG_DIR/agent.env"
if [ -f "$cfg" ]; then
  perms=$(ls -l "$cfg" | cut -c1-10)
  [ "$perms" = "-rw-------" ] && pass "agent config $cfg (mode 600)" || warn "agent config $cfg has permissions $perms (expected -rw-------)"
  server=$(grep -E '^SHAREDCOMPUTE_SERVER=' "$cfg" | tail -1 | cut -d= -f2-)
  case "$server" in
    https://*|http://127.0.0.1*|http://localhost*|"") note "server: ${server:-<not set>}" ;;
    *) fail "agent config points at plain HTTP '$server'; the agent will refuse it (use https://)" ;;
  esac
  gpus=$(grep -E '^SHAREDCOMPUTE_GPUS=' "$cfg" | tail -1 | cut -d= -f2-)
  cpu=$(grep -E '^SHAREDCOMPUTE_OFFER_CPU=' "$cfg" | tail -1 | cut -d= -f2-)
  note "offered by config: GPUs '${gpus:-none}', CPU '${cpu:-0}'"
  token=$(grep -E '^SHAREDCOMPUTE_TOKEN_FILE=' "$cfg" | tail -1 | cut -d= -f2-)
  token=${token/#\~/$HOME}
  if [ -n "$token" ] && [ -f "$token" ]; then
    tperms=$(ls -l "$token" | cut -c1-10)
    [ "$tperms" = "-rw-------" ] && pass "node token file present (mode 600; contents not shown)" || fail "node token $token has permissions $tperms: run chmod 600"
  else
    note "no node token file yet"
  fi
else
  note "no agent config at $cfg (provider machines: scripts/bootstrap-agent.sh)"
fi

section "Hardware (read-only diagnose; nothing is offered or executed)"
if [ -n "$AGENT_PY" ]; then
  SHAREDCOMPUTE_AGENT_ENV="$CONFIG_DIR/agent.env" "$AGENT_PY" -m sharedcompute_agent diagnose 2>&1 | sed 's/^/        /'
  pass "diagnose ran"
fi
if [ "$OS" = Linux ]; then
  command -v nvidia-smi >/dev/null 2>&1 && note "nvidia-smi: $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | tr '\n' ';')" \
    || note "nvidia-smi not found (no NVIDIA proprietary driver)"
  lsmod 2>/dev/null | grep -q '^nouveau' && warn "nouveau driver loaded: no CUDA"
fi

section "Repository hygiene"
if sh scripts/check-repo-hygiene.sh >/tmp/sc-doctor-hyg.$$ 2>&1; then pass "no secrets, databases or local files tracked"; else fail "hygiene check failed:"; sed 's/^/        /' /tmp/sc-doctor-hyg.$$; fi
rm -f /tmp/sc-doctor-hyg.$$

printf '\nSummary: %d pass, %d warn, %d fail\n' "$PASS" "$WARN" "$FAIL"
[ "$FAIL" -eq 0 ]
