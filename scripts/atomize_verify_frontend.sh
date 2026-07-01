#!/usr/bin/env bash
# Frontend-only atomize verification — ap-web per bytedesk-atomize SKILL.md scope.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH="${ATOMIZE_SCRATCH:-/tmp/grok-goal-206e2ee288a2/implementer}"
mkdir -p "$SCRATCH"

cd "$ROOT"
HEAD="$(git rev-parse HEAD)"
BRANCH="$(git branch --show-current)"

echo "== atomize_verify_frontend @ $BRANCH $HEAD ==" | tee "$SCRATCH/atomize-verify-frontend-run.txt"

python3 scripts/atomize_survey.py 2>&1 | tee "$SCRATCH/atomize-final-report.txt"
SURVEY_EXIT="${PIPESTATUS[0]}"
echo "survey_exit=$SURVEY_EXIT" >> "$SCRATCH/atomize-final-report.txt"

# Gate on frontend categories only (exclude python section).
FRONTEND_P1=0
if grep -E '^### (page_entries|page_organisms|shell_entries|components|ai_elements) \(P1 offenders: [1-9]' \
    "$SCRATCH/atomize-final-report.txt" >/dev/null; then
  FRONTEND_P1=1
fi
if grep -qE '^### inline_page_entries: [1-9]' "$SCRATCH/atomize-final-report.txt"; then
  FRONTEND_P1=1
fi

uv run python -m pytest tests/test_atomize_structure.py \
  -k "page or shell or component or ai_element or inline" \
  -v --tb=no 2>&1 | tee "$SCRATCH/atomize-structure-tests-frontend.log"
STRUCTURE_EXIT="${PIPESTATUS[0]}"

(
  cd ap-web
  npm ci >/dev/null 2>&1 || npm ci 2>&1 | tail -3
  npm run build
) 2>&1 | tee "$SCRATCH/ap-web-build.log"
BUILD_EXIT="${PIPESTATUS[0]}"

{
  echo "=== Frontend line counts (plan step 5) ==="
  echo "--- page entries ---"
  wc -l ap-web/src/pages/*.tsx 2>/dev/null | sort -rn | head -20
  echo "--- page organisms ---"
  wc -l ap-web/src/pages/organisms/**/*.tsx 2>/dev/null | sort -rn | head -15
  echo "--- shell entries ---"
  wc -l ap-web/src/shell/*.tsx 2>/dev/null | grep -v test | sort -rn | head -15
} > "$SCRATCH/line-counts-final.txt"

{
  echo "# Final Atomize Verification — LIVE (frontend scope)"
  echo "Date: $(date -Iseconds)"
  echo "Branch: $BRANCH"
  echo "HEAD: $HEAD"
  echo "scope: ap-web/src only (bytedesk-atomize SKILL.md)"
  echo "survey_exit: $SURVEY_EXIT"
  echo "frontend_p1: $FRONTEND_P1"
  echo "structure_exit: $STRUCTURE_EXIT"
  echo "build_exit: $BUILD_EXIT"
  echo ""
  grep SYNTHESIS "$SCRATCH/atomize-final-report.txt" || true
  echo ""
  echo "Frontend categories:"
  grep -E '^### (page_entries|page_organisms|shell_entries|components|ai_elements|inline_page)' \
    "$SCRATCH/atomize-final-report.txt" || true
} > "$SCRATCH/final-atomize-verification.txt"

FAIL=0
[[ "$SURVEY_EXIT" -eq 0 ]] || FAIL=1
[[ "$FRONTEND_P1" -eq 0 ]] || FAIL=1
[[ "$STRUCTURE_EXIT" -eq 0 ]] || FAIL=1
[[ "$BUILD_EXIT" -eq 0 ]] || FAIL=1

if [[ "$FAIL" -eq 0 ]]; then
  echo "OK: atomize_verify_frontend passed" | tee -a "$SCRATCH/atomize-verify-frontend-run.txt"
  exit 0
fi

echo "FAIL: atomize_verify_frontend — see $SCRATCH" | tee -a "$SCRATCH/atomize-verify-frontend-run.txt"
exit 1