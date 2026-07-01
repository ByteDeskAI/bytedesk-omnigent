#!/usr/bin/env bash
# Preflight gate for atomize goal completion — validates branch + diff purity.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BRANCH="$(git branch --show-current)"
if [[ "$BRANCH" != "feature/atomize-repo" ]]; then
  echo "FAIL: expected branch feature/atomize-repo, got ${BRANCH:-<detached>}" >&2
  exit 1
fi

git fetch origin develop --prune 2>/dev/null || true

FORBIDDEN=0
while IFS=$'\t' read -r status path; do
  [[ -z "$path" ]] && continue
  if [[ "$path" == deploy/* ]] \
    || [[ "$path" == *"website-"* ]] \
    || [[ "$path" == "bytedesk_omnigent/tasks/seed.py" ]]; then
    echo "FAIL: forbidden path in diff vs origin/develop: $path (${status})" >&2
    FORBIDDEN=1
  fi
  if [[ "$path" == "omnigent/server/routes/sessions.py" && "$status" != "D" ]]; then
    echo "FAIL: sessions.py must be deleted (decomposed to sessions/ package), got status ${status}" >&2
    FORBIDDEN=1
  fi
done < <(git diff --name-status origin/develop)

if [[ "$FORBIDDEN" -ne 0 ]]; then
  exit 1
fi

if [[ -f omnigent/server/routes/sessions.py ]]; then
  echo "FAIL: omnigent/server/routes/sessions.py must be decomposed package (sessions/)" >&2
  exit 1
fi

if [[ ! -d omnigent/server/routes/sessions ]]; then
  echo "FAIL: expected omnigent/server/routes/sessions/ package" >&2
  exit 1
fi

ATOMIZE_MARKERS=0
while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  case "$path" in
    ap-web/src/*|omnigent/*|bytedesk_omnigent/*|scripts/atomize_*|tests/test_atomize_structure.py)
      ATOMIZE_MARKERS=1
      break
      ;;
  esac
done < <(git diff --name-only origin/develop)

if [[ "$ATOMIZE_MARKERS" -eq 0 ]]; then
  echo "FAIL: diff vs origin/develop has no atomize-related paths" >&2
  exit 1
fi

echo "OK: atomize preflight passed (branch=${BRANCH}, $(git diff --name-only origin/develop | wc -l) files vs origin/develop)"