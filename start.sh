#!/bin/sh
# Start SharedCompute on http://127.0.0.1:8765 (loopback only). First run sets up .venv via
# scripts/bootstrap-dev.sh. Extra arguments go to `python -m sharedcompute serve` (e.g. --port 9000).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sharedcompute' >/dev/null 2>&1; then
  scripts/bootstrap-dev.sh --skip-tests
fi
exec .venv/bin/python -m sharedcompute serve "$@"
