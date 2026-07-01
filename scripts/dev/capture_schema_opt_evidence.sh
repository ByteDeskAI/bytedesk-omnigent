#!/usr/bin/env bash
# Capture verification-plan evidence for bdp2610 schema optimizations.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRATCH="${1:-/tmp/grok-goal-b0d380ec040d/implementer}"
mkdir -p "$SCRATCH"

cd "$ROOT"

if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python3"
fi

echo "=== schema opt evidence capture $(date -Iseconds) ===" | tee "$SCRATCH/land-evidence.log"
echo "python=$PY" | tee -a "$SCRATCH/land-evidence.log"

"$PY" scripts/dev/verify_schema_opt_evidence.py \
  --output "$SCRATCH/inspector-transcript.log" \
  --pg-skip-output "$SCRATCH/pg-skip.log" \
  2>&1 | tee -a "$SCRATCH/land-evidence.log"

export SCHEMA_OPT_SCRATCH_DIR="$SCRATCH"
"$PY" -m pytest tests/db/test_migrations_sqlite_safe.py tests/db/ -q \
  2>&1 | tee "$SCRATCH/db-tests.log"

echo "evidence written under $SCRATCH" | tee -a "$SCRATCH/land-evidence.log"