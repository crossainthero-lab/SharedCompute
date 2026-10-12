#!/bin/sh
# Fail if tracked files or history contain tokens, keys, databases or local artefacts.
# Run before pushing: sh scripts/check-repo-hygiene.sh
set -eu
cd "$(dirname "$0")/.."
status=0

tracked=$(git ls-files)
bad_files=$(printf '%s\n' "$tracked" | grep -E '\.(sqlite3|db|pem|key|token|p12|pfx)$|(^|/)\.env($|\.)|(^|/)data/|\.DS_Store$|__pycache__|\.egg-info/' \
  | grep -v '^agent/tests/fixtures/' || true)
if [ -n "$bad_files" ]; then
  echo "Tracked files that should not be committed:"; echo "$bad_files"; status=1
fi

# Real tokens are prefix + 43 url-safe characters. Test code builds fake ones by concatenation.
pattern='(scn|scs)_[A-Za-z0-9_-]{40,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}'
if git grep -nE "$pattern" -- . >/dev/null 2>&1; then
  echo "Secret-like strings in tracked files:"; git grep -nE "$pattern" -- . ; status=1
fi
if git log --all -p | grep -nE "$pattern" >/dev/null 2>&1; then
  echo "Secret-like strings in git history (rewrite history before publishing):"
  git log --all -p | grep -nE "$pattern" | head -20; status=1
fi

large=$(git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' \
  | awk '$1 == "blob" && $2 > 1000000 { print $3 " (" $2 " bytes)" }')
if [ -n "$large" ]; then
  echo "Blobs over 1 MB in history:"; echo "$large"; status=1
fi

[ "$status" -eq 0 ] && echo "Repository hygiene: OK"
exit "$status"
