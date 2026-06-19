#!/usr/bin/env bash
# Parity harness driver (BDP-2326, ADR-0145 §parity).
#
# Runs a slice of the test suite TWICE — once with a named feature flag
# OFF (the legacy / baseline path) and once with it ON (the new
# abstraction path) — then diffs the two JUnit reports. The exit code is
# the parity verdict: 0 when both runs agree (same pass/fail per nodeid),
# non-zero on ANY divergence.
#
# This is the dual-path executor for the Phase 0 characterization &
# parity harness. It does NOT capture golden outputs (that requires the
# live suite — out of scope for the scaffold); it proves that flipping
# the seam under test does not change observable test outcomes.
#
# Usage:
#   scripts/test_parity.sh [FLAG_NAME] [PYTEST_PATHS...]
#
#   FLAG_NAME      Environment variable toggled OFF (unset) then ON ("1").
#                  Default: OMNIGENT_ABSTRACTION_SEAM (the seam introduced
#                  by the BDP-2323 abstraction epic). Override to parity
#                  any boolean feature flag.
#   PYTEST_PATHS   Test paths/args passed straight to pytest. Default:
#                  tests/parity (the characterization skeletons).
#
# Examples:
#   scripts/test_parity.sh
#   scripts/test_parity.sh OMNIGENT_ABSTRACTION_SEAM tests/stores
#   PARITY_PYTEST_ARGS="-x -q" scripts/test_parity.sh
#
# Environment:
#   PARITY_PYTEST_ARGS   Extra args appended to every pytest invocation
#                        (default: "-q -p no:cacheprovider").
#   PARITY_OUT_DIR       Where the two JUnit reports + diff land
#                        (default: a mktemp dir, cleaned on exit).
set -euo pipefail

FLAG_NAME="${1:-OMNIGENT_ABSTRACTION_SEAM}"
shift || true
PYTEST_PATHS=("$@")
if [[ ${#PYTEST_PATHS[@]} -eq 0 ]]; then
  PYTEST_PATHS=("tests/parity")
fi

# Resolve repo root from this script's location so the driver works from
# any cwd (CI, worktree, canonical checkout).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

# Match the suite's CI invocation: `uv run pytest` when uv is present,
# else a plain `python -m pytest` for environments that already activated
# the venv. Word-split PARITY_PYTEST_ARGS like CI word-splits matrix args.
read -r -a _EXTRA_ARGS <<<"${PARITY_PYTEST_ARGS:--q -p no:cacheprovider}"
if command -v uv >/dev/null 2>&1; then
  PYTEST_CMD=(uv run pytest)
else
  PYTEST_CMD=(python -m pytest)
fi

OUT_DIR="${PARITY_OUT_DIR:-}"
_OWNED_OUT_DIR=0
if [[ -z "${OUT_DIR}" ]]; then
  OUT_DIR="$(mktemp -d -t omnigent-parity-XXXXXX)"
  _OWNED_OUT_DIR=1
fi
mkdir -p "${OUT_DIR}"

cleanup() {
  if [[ "${_OWNED_OUT_DIR}" -eq 1 ]]; then
    rm -rf "${OUT_DIR}"
  fi
}
trap cleanup EXIT

OFF_REPORT="${OUT_DIR}/junit-off.xml"
ON_REPORT="${OUT_DIR}/junit-on.xml"

# One pytest run. $1 = junit path; the flag is set/unset by the caller via
# the surrounding env. A pytest exit of 0 (all passed) or 1 (some failed)
# is a *completed* run we can diff; anything else (2=usage, 3=internal,
# 4=no-tests, 5=interrupted) is a harness error that aborts the parity run.
run_suite() {
  local junit="$1"
  local rc=0
  ( cd "${REPO_ROOT}" && "${PYTEST_CMD[@]}" "${PYTEST_PATHS[@]}" \
      "${_EXTRA_ARGS[@]}" \
      --junitxml="${junit}" ) || rc=$?
  if [[ "${rc}" -gt 1 ]]; then
    echo "test_parity: pytest exited ${rc} (harness error, not a test failure) — aborting" >&2
    exit 3
  fi
  return 0
}

echo "==> Parity flag: ${FLAG_NAME}"
echo "==> Paths:       ${PYTEST_PATHS[*]}"
echo

echo "==> Run 1/2: ${FLAG_NAME} OFF (baseline path)"
( unset "${FLAG_NAME}"; run_suite "${OFF_REPORT}" )

echo
echo "==> Run 2/2: ${FLAG_NAME} ON (abstraction path)"
( export "${FLAG_NAME}=1"; run_suite "${ON_REPORT}" )

echo
echo "==> Diffing per-test outcomes (OFF vs ON)"

# Reduce each JUnit report to a sorted `nodeid<TAB>outcome` table so the
# diff is order-independent and ignores timing/duration noise. A testcase
# is `fail` if it carries a <failure> or <error> child, `skip` for
# <skipped>, else `pass`. Pure-stdlib Python — no lxml dependency.
outcomes() {
  python3 - "$1" <<'PY'
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]
root = ET.parse(path).getroot()
# JUnit root is either <testsuites> or a single <testsuite>.
suites = root.iter("testsuite")
rows = []
for suite in suites:
    for case in suite.iter("testcase"):
        node = "{}::{}".format(case.get("classname", ""), case.get("name", ""))
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "fail"
        elif case.find("skipped") is not None:
            outcome = "skip"
        else:
            outcome = "pass"
        rows.append((node, outcome))
for node, outcome in sorted(rows):
    print("{}\t{}".format(node, outcome))
PY
}

OFF_TABLE="${OUT_DIR}/outcomes-off.tsv"
ON_TABLE="${OUT_DIR}/outcomes-on.tsv"
outcomes "${OFF_REPORT}" >"${OFF_TABLE}"
outcomes "${ON_REPORT}" >"${ON_TABLE}"

if diff -u "${OFF_TABLE}" "${ON_TABLE}"; then
  echo
  echo "==> PARITY OK: ${FLAG_NAME} OFF and ON produced identical per-test outcomes."
  exit 0
fi

echo
echo "==> PARITY DIVERGENCE: ${FLAG_NAME} changed at least one test outcome (diff above)." >&2
echo "    OFF report: ${OFF_REPORT}" >&2
echo "    ON  report: ${ON_REPORT}" >&2
exit 1
