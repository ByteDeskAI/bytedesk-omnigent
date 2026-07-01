#!/usr/bin/env bash
# Retired-spine smoke harness.
#
# The OOP/DI spine strangler flags have been cut over. There is no remaining
# OFF-vs-ON runtime pair to diff in this slice, so this script preserves the
# old operator entry point while running the canonical spine/runtime tests once.
#
# Usage:
#   scripts/test_parity_combined.sh [PYTEST_PATHS...]
#
# Environment:
#   PARITY_PYTEST_ARGS    Extra args appended to pytest
#                         (default: "-q -p no:cacheprovider -p no:warnings").
#   PARITY_EXTRA_DESELECT Space-separated extra "file::test" nodeids to
#                         deselect.
set -euo pipefail

DESELECT=()
# shellcheck disable=SC2206
if [[ -n "${PARITY_EXTRA_DESELECT:-}" ]]; then
  DESELECT+=(${PARITY_EXTRA_DESELECT})
fi

PYTEST_PATHS=("$@")
if [[ ${#PYTEST_PATHS[@]} -eq 0 ]]; then
  PYTEST_PATHS=(
    tests/stores/test_store_lifecycle.py
    tests/stores/test_store_factory.py
    tests/runner/test_tool_execution_context.py
    tests/runner/test_tool_dispatcher_registry.py
    tests/runner/test_tool_dispatch_execution_errors.py
    tests/runner/test_tool_dispatch_parse_helpers.py
    tests/server/test_lifespan_phases.py
    tests/extensions/test_abstraction_spine_contract.py
    tests/parity
  )
fi

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

echo "==> OOP/DI spine flags are retired; running canonical smoke once"
echo "==> Paths: ${PYTEST_PATHS[*]}"
if [[ ${#DESELECT[@]} -gt 0 ]]; then
  echo "==> Deselected:"
  for nodeid in "${DESELECT[@]}"; do echo "      - ${nodeid}"; done
fi
echo

( cd "${REPO_ROOT}" && "${PYTEST_CMD[@]}" "${PYTEST_PATHS[@]}" \
    "${_DESELECT_ARGS[@]}" \
    "${_EXTRA_ARGS[@]}" )

echo
echo "==> SPINE CUTOVER OK"
