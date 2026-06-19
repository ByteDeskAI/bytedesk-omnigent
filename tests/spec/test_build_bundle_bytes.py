"""Tests for the deterministic agent-image bundle builder."""

from __future__ import annotations

from pathlib import Path

from omnigent.spec.tar_utils import build_bundle_bytes, extract_safe


def _make_image(root: Path) -> Path:
    """Write a small agent image tree under *root*."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("spec_version: 1\nname: demo\n")
    (root / "AGENTS.md").write_text("You are demo.\n")
    skill = root / "skills" / "deep-search"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deep-search\ndescription: x\n---\nbody\n")
    return root


def test_deterministic_same_content_same_bytes(tmp_path: Path) -> None:
    a = _make_image(tmp_path / "a")
    b = _make_image(tmp_path / "b")
    # Different directory paths, identical content → identical bytes
    # (so sha256 is a true content address, independent of build order/fs).
    assert build_bundle_bytes(a) == build_bundle_bytes(b)


def test_different_content_different_bytes(tmp_path: Path) -> None:
    a = _make_image(tmp_path / "a")
    b = _make_image(tmp_path / "b")
    (b / "config.yaml").write_text("spec_version: 1\nname: demo\nparams:\n  k: v\n")
    assert build_bundle_bytes(a) != build_bundle_bytes(b)


def test_round_trip_extract_preserves_files(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "src")
    data = build_bundle_bytes(src)
    dest = tmp_path / "out"
    extract_safe(data, dest)
    assert (dest / "config.yaml").read_text() == "spec_version: 1\nname: demo\n"
    assert (dest / "AGENTS.md").read_text() == "You are demo.\n"
    assert (dest / "skills" / "deep-search" / "SKILL.md").exists()


def test_rebuild_after_extract_is_idempotent(tmp_path: Path) -> None:
    # Build → extract → rebuild must reproduce the same bytes, so a
    # read-modify-rewrite that changes nothing short-circuits as a no-op.
    src = _make_image(tmp_path / "src")
    first = build_bundle_bytes(src)
    dest = tmp_path / "out"
    extract_safe(first, dest)
    assert build_bundle_bytes(dest) == first
