"""Parity-harness helpers: golden capture/replay + the abstraction flag.

Shared by the characterization skeletons (``test_characterization_skeletons``)
and exercised directly by ``test_parity_harness``. Pure-stdlib, no LLM /
network — the parity lane is a cheap merge gate (see ``README.md`` § CI
budget).

Two record/replay primitives keep characterization tests deterministic:

- :func:`abstraction_seam_enabled` reads the named feature flag that
  :file:`scripts/test_parity.sh` toggles OFF then ON. Default OFF so
  production behaviour is unchanged until the seam flips.
- :func:`assert_or_capture_golden` is the capture-once / replay-forever
  contract assertion. With ``OMNIGENT_PARITY_CAPTURE=1`` it writes the
  observed contract to ``_golden/<name>.json``; otherwise it loads that
  file and asserts equality. Golden payloads ride as JSON-in-a-flat-file
  (mirroring the dual-DB JSON-in-Text rule) so a captured baseline
  round-trips through ``git diff`` with zero noise.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Canonical feature flag for the BDP-2323 abstraction epic. The dual-path
# driver (scripts/test_parity.sh) runs each parity slice with this OFF
# (unset) then ON ("1"). Any other flag name can be passed to the driver;
# this is the default the characterization tests read.
ABSTRACTION_SEAM_FLAG = "OMNIGENT_ABSTRACTION_SEAM"

# Env switch that flips :func:`assert_or_capture_golden` from replay
# (assert) to capture (write). Never set in CI — capture is a
# developer/maintainer action recorded into the golden files and reviewed
# in the PR diff.
GOLDEN_CAPTURE_FLAG = "OMNIGENT_PARITY_CAPTURE"

_GOLDEN_DIR = Path(__file__).parent / "_golden"

# Generated ids are non-deterministic per run; normalize the prefixed
# forms (conv_…, ag_…, pol_…, runner_…) to a stable placeholder so a
# golden file is byte-stable across captures.
_ID_PREFIXES = ("conv", "ag", "pol", "runner", "msg", "sess")
_ID_RE = re.compile(r"\b(" + "|".join(_ID_PREFIXES) + r")_[A-Za-z0-9]+\b")


def abstraction_seam_enabled(flag: str = ABSTRACTION_SEAM_FLAG) -> bool:
    """Return whether the abstraction seam is selected (flag ON).

    :param flag: Env var name to read. Defaults to the epic flag
        :data:`ABSTRACTION_SEAM_FLAG`.
    :returns: ``True`` only when the var is exactly ``"1"``; OFF (the
        legacy / baseline path) for unset or any other value.
    """
    return os.environ.get(flag) == "1"


def golden_capture_enabled() -> bool:
    """:returns: ``True`` when :data:`GOLDEN_CAPTURE_FLAG` is ``"1"``."""
    return os.environ.get(GOLDEN_CAPTURE_FLAG) == "1"


def normalize_contract(value: Any) -> Any:
    """Strip per-run nondeterminism from a captured contract value.

    Generated ids collapse to ``<id>``; integer timestamps under the
    well-known keys (``created_at`` / ``updated_at``) collapse to ``0``.
    Recurses through dicts and lists so a nested entity dump is stable.

    :param value: The raw contract output (dict, list, scalar).
    :returns: A structurally identical value with ids/timestamps
        normalized.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in ("created_at", "updated_at") and isinstance(item, int):
                out[key] = 0
            else:
                out[key] = normalize_contract(item)
        return out
    if isinstance(value, list):
        return [normalize_contract(item) for item in value]
    if isinstance(value, str):
        return _ID_RE.sub("<id>", value)
    return value


def _golden_path(name: str) -> Path:
    return _GOLDEN_DIR / f"{name}.json"


def assert_or_capture_golden(name: str, observed: Any) -> None:
    """Capture-once / replay-forever golden contract assertion.

    In capture mode (:func:`golden_capture_enabled`) the normalized
    ``observed`` value is written to ``_golden/<name>.json`` and the call
    returns without asserting — this is how a baseline is recorded on the
    legacy path. Otherwise the golden file is loaded and the normalized
    ``observed`` value is asserted equal to it.

    :param name: Golden file stem, one per subsystem contract.
    :param observed: The contract output to capture or replay.
    :raises AssertionError: In replay mode, when ``observed`` diverges
        from the captured baseline.
    :raises FileNotFoundError: In replay mode, when no baseline has been
        captured yet — the caller (a TODO-marked skeleton) should skip
        until capture lands.
    """
    payload = normalize_contract(observed)
    path = _golden_path(name)
    if golden_capture_enabled():
        _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return
    golden = json.loads(path.read_text())
    assert payload == golden, (
        f"parity golden divergence for {name!r}: observed != _golden/{name}.json"
    )


def golden_exists(name: str) -> bool:
    """:returns: ``True`` when a captured baseline file exists for *name*."""
    return _golden_path(name).exists()
