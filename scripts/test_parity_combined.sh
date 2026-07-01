#!/usr/bin/env bash
# Remaining-strangler parity harness (BDP-2343, ADR-0145 §parity).
#
# scripts/test_parity.sh diffs one strangler flag OFF vs ON at a time. This
# driver runs a representative slice TWICE — once with the remaining gate OFF
# and once with it ON — then diffs the two JUnit reports. Exit 0 iff every test
# has the SAME per-nodeid outcome under both configurations.
#
# Server DI and lifespan phases are canonical code paths now. ServiceRegistry,
# ToolExecutionContext, and ToolDispatcher-registry rollout flags are also
# retired. The remaining gate covered by this harness is:
#   OMNIGENT_STORE_LIFECYCLE_HOOKS
#
# Usage:
#   scripts/test_parity_combined.sh [PYTEST_PATHS...]
#
#   PYTEST_PATHS   Test paths/args passed straight to pytest. Default: the
#                  representative slice below (stores + tool dispatch +
#                  service-registry/lifespan + parity skeletons).
#
# Environment:
#   PARITY_PYTEST_ARGS   Extra args appended to every pytest invocation
#                        (default: "-q -p no:cacheprovider -p no:warnings").
#   PARITY_OUT_DIR       Where the two JUnit reports + diff land
#                        (default: a mktemp dir, cleaned on exit).
#   PARITY_EXTRA_DESELECT Space-separated extra "file::test" nodeids to
#                        deselect on both runs (appended to the built-in one).
set -euo pipefail

COMBINED_FLAGS=(
  OMNIGENT_STORE_LIFECYCLE_HOOKS
)

DESELECT=()
# shellcheck disable=SC2206
if [[ -n "${PARITY_EXTRA_DESELECT:-}" ]]; then
  DESELECT+=(${PARITY_EXTRA_DESELECT})
fi

PYTEST_PATHS=("$@")
if [[ ${#PYTEST_PATHS[@]} -eq 0 ]]; then
  # Representative slice — store lifecycle plus nearby canonical runtime seams:
  #   STORE_LIFECYCLE_HOOKS        → tests/stores
  #   canonical runner tool dispatch → tests/runner tool-dispatch
  #   canonical lifespan phases      → tests/server/test_lifespan_phases.py
  #   retired-flag compatibility     → tests/parity
  # tests/extensions/test_abstraction_spine_contract.py is deliberately NOT
  # here: it is a pure source-AST scan of app.py (flag-independent — it never
  # reads any of the combined flags) so it exercises no runtime seam, and it is
  # currently red on develop for an unrelated app.state drift. Add it back only
  # once that drift is re-pinned; it contributes nothing to a flag-parity diff.
  PYTEST_PATHS=(
    tests/stores
    tests/runner/test_tool_execution_context.py
    tests/runner/test_tool_dispatcher_registry.py
    tests/runner/test_tool_dispatch_execution_errors.py
    tests/runner/test_tool_dispatch_parse_helpers.py
    tests/server/test_lifespan_phases.py
    tests/parity
  )
fi

# Resolve repo root from this script's location so the driver works from any cwd.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

read -r -a _EXTRA_ARGS <<<"${PARITY_PYTEST_ARGS:--q -p no:cacheprovider -p no:warnings}"
if command -v uv >/dev/null 2>&1; then
  PYTEST_CMD=(uv run pytest)
else
  PYTEST_CMD=(python -m pytest)
fi

_DESELECT_ARGS=()
for nodeid in "${DESELECT[@]}"; do
  _DESELECT_ARGS+=(--deselect "${nodeid}")
done

OUT_DIR="${PARITY_OUT_DIR:-}"
_OWNED_OUT_DIR=0
if [[ -z "${OUT_DIR}" ]]; then
  OUT_DIR="$(mktemp -d -t omnigent-parity-combined-XXXXXX)"
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

# One pytest run. $1 = junit path. Flags are set/unset by the caller via the
# surrounding env. pytest exit 0 (all passed) or 1 (some failed) is a completed
# run we can diff; anything else aborts as a harness error.
run_suite() {
  local junit="$1"
  local rc=0
  ( cd "${REPO_ROOT}" && "${PYTEST_CMD[@]}" "${PYTEST_PATHS[@]}" \
      "${_DESELECT_ARGS[@]}" \
      "${_EXTRA_ARGS[@]}" \
      --junitxml="${junit}" ) || rc=$?
  if [[ "${rc}" -gt 1 ]]; then
    echo "test_parity_combined: pytest exited ${rc} (harness error, not a test failure) — aborting" >&2
    exit 3
  fi
  return 0
}

echo "==> Remaining gate(s):    ${COMBINED_FLAGS[*]}"
echo "==> Paths:                ${PYTEST_PATHS[*]}"
echo "==> Deselected (both runs, superseded by registry precedence):"
for nodeid in "${DESELECT[@]}"; do echo "      - ${nodeid}"; done
echo

echo "==> Run 1/2: remaining gate(s) OFF (baseline)"
(
  for flag in "${COMBINED_FLAGS[@]}"; do unset "${flag}" || true; done
  run_suite "${OFF_REPORT}"
)

echo
echo "==> Run 2/2: remaining gate(s) ON"
(
  for flag in "${COMBINED_FLAGS[@]}"; do export "${flag}=1"; done
  run_suite "${ON_REPORT}"
)

echo
echo "==> Diffing per-test outcomes (flags-OFF vs flags-ON)"

# Reduce each JUnit report to a sorted `nodeid<TAB>outcome` table so the diff is
# order-independent and ignores timing noise. Pure stdlib — mirrors test_parity.sh.
outcomes() {
  python3 - "$1" <<'PY'
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]
root = ET.parse(path).getroot()
rows = []
for suite in root.iter("testsuite"):
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
  echo "==> PARITY OK: remaining gate(s) ON produced identical per-test outcomes"
  echo "    vs the OFF baseline across $(wc -l <"${OFF_TABLE}") tests."
  exit 0
fi

echo
echo "==> PARITY DIVERGENCE: the remaining gate config changed at least one" >&2
echo "    test outcome (diff above)." >&2
echo "    OFF report: ${OFF_REPORT}" >&2
echo "    ON  report: ${ON_REPORT}" >&2
exit 1
