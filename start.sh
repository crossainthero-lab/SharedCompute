#!/bin/sh
# Start SharedCompute on http://127.0.0.1:8765. Creates .venv and installs dependencies on first run.
# Extra arguments are passed to `python -m sharedcompute serve` (e.g. --port 9000).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv and installing dependencies (first run only)..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e '.[dev]'
fi
exec .venv/bin/python -m sharedcompute serve "$@"
