"""Unit tests for the parity-harness helpers themselves (BDP-2326).

The parity infrastructure is a merge-gate safety net, so the safety net
is itself tested: flag reading, contract normalization (ids/timestamps),
and the capture-once / replay-forever golden round-trip.
"""

from __future__ import annotations

import json

import pytest

from tests.parity import _harness

# ── abstraction_seam_enabled ─────────────────────────────────────────


def test_seam_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_harness.ABSTRACTION_SEAM_FLAG, raising=False)
    assert _harness.abstraction_seam_enabled() is False


def test_seam_on_only_for_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_harness.ABSTRACTION_SEAM_FLAG, "1")
    assert _harness.abstraction_seam_enabled() is True
    # Any non-"1" value is OFF — only the explicit ON flips it.
    monkeypatch.setenv(_harness.ABSTRACTION_SEAM_FLAG, "true")
    assert _harness.abstraction_seam_enabled() is False


def test_seam_reads_custom_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_OTHER_SEAM", "1")
    assert _harness.abstraction_seam_enabled("OMNIGENT_OTHER_SEAM") is True


# ── normalize_contract ───────────────────────────────────────────────


def test_normalize_strips_generated_ids() -> None:
    raw = {"id": "conv_AbC123", "parent": "ag_xyz789", "name": "keep-me"}
    assert _harness.normalize_contract(raw) == {
        "id": "<id>",
        "parent": "<id>",
        "name": "keep-me",
    }


def test_normalize_zeroes_timestamps_but_not_other_ints() -> None:
    raw = {"created_at": 1700000000, "updated_at": 1700000001, "count": 7}
    assert _harness.normalize_contract(raw) == {
        "created_at": 0,
        "updated_at": 0,
        "count": 7,
    }


def test_normalize_recurses_into_nested_structures() -> None:
    raw = {"items": [{"id": "pol_a1"}, {"id": "pol_b2"}]}
    assert _harness.normalize_contract(raw) == {
        "items": [{"id": "<id>"}, {"id": "<id>"}],
    }


# ── assert_or_capture_golden ─────────────────────────────────────────


def test_capture_then_replay_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    # Redirect the golden dir into a temp path so the test never writes a
    # real baseline file into the repo.
    monkeypatch.setattr(_harness, "_GOLDEN_DIR", tmp_path)

    # Capture mode writes the normalized payload and does not assert.
    monkeypatch.setenv(_harness.GOLDEN_CAPTURE_FLAG, "1")
    _harness.assert_or_capture_golden("demo", {"id": "conv_abc", "ok": True})
    assert _harness.golden_exists("demo")
    on_disk = json.loads((tmp_path / "demo.json").read_text())
    assert on_disk == {"id": "<id>", "ok": True}  # id normalized

    # Replay mode asserts equality against the captured baseline. A
    # different generated id still matches because it normalizes the same.
    monkeypatch.delenv(_harness.GOLDEN_CAPTURE_FLAG, raising=False)
    _harness.assert_or_capture_golden("demo", {"id": "conv_zzz999", "ok": True})


def test_replay_divergence_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(_harness, "_GOLDEN_DIR", tmp_path)
    (tmp_path / "demo.json").write_text(json.dumps({"ok": True}) + "\n")
    monkeypatch.delenv(_harness.GOLDEN_CAPTURE_FLAG, raising=False)
    with pytest.raises(AssertionError):
        _harness.assert_or_capture_golden("demo", {"ok": False})


def test_golden_exists_false_when_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(_harness, "_GOLDEN_DIR", tmp_path)
    assert _harness.golden_exists("never_captured") is False
