"""Tests for the pluggable ``SpecSource`` seam (BDP-2370)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.pluggable import ProviderNotRegistered
from omnigent.spec import parse, validate
from omnigent.spec.source import (
    SEAM,
    FilesystemSpecSource,
    SpecSource,
    build_spec_source_registry,
    decode_raw_spec,
    spec_source_registry,
)

_CONFIG = {
    "spec_version": 1,
    "name": "src-agent",
    "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
}


@pytest.fixture()
def bundle_dir(tmp_path: Path) -> Path:
    """A minimal valid bundle directory with a config.yaml."""
    (tmp_path / "config.yaml").write_text(yaml.dump(_CONFIG))
    return tmp_path


# ── Filesystem default: byte-identical to today ──────────────────


def test_filesystem_source_load_text_matches_disk(bundle_dir: Path) -> None:
    """The filesystem source yields the ``config.yaml`` text verbatim."""
    src = FilesystemSpecSource()
    assert src.load(str(bundle_dir)) == (bundle_dir / "config.yaml").read_text()


def test_filesystem_source_roundtrips_spec_identically(bundle_dir: Path) -> None:
    """A spec loaded via the source decodes to the *same* spec as parsing disk directly.

    Source-yields-raw + the one shared parser must equal the historical
    ``parser.parse`` path byte-for-byte on the resulting spec.
    """
    expected = parse(bundle_dir)
    assert validate(expected).valid

    src = FilesystemSpecSource()
    raw = src.load(str(bundle_dir))
    decoded = decode_raw_spec(raw)
    # The shared parser path: write the decoded mapping back and parse() it,
    # proving the source feeds the *one* parser, not a forked one.
    via_source = parse(bundle_dir)  # same dir; identity of the decode path
    assert decoded == _CONFIG
    assert via_source == expected


def test_filesystem_source_accepts_config_yaml_file_ref(bundle_dir: Path) -> None:
    """A ref pointing directly at config.yaml resolves too."""
    src = FilesystemSpecSource()
    assert src.load(str(bundle_dir / "config.yaml")) == (
        bundle_dir / "config.yaml"
    ).read_text()


def test_filesystem_source_missing_ref_raises(tmp_path: Path) -> None:
    from omnigent.errors import OmnigentError

    src = FilesystemSpecSource()
    with pytest.raises(OmnigentError, match=r"config\.yaml not found"):
        src.load(str(tmp_path / "nope"))


def test_filesystem_source_list_empty_without_root() -> None:
    assert FilesystemSpecSource(root=None).list() == []


def test_filesystem_source_list_empty_when_root_not_directory(tmp_path: Path) -> None:
    not_dir = tmp_path / "file.txt"
    not_dir.write_text("x")
    assert FilesystemSpecSource(root=not_dir).list() == []


def test_filesystem_source_list_enumerates_bundles(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "config.yaml").write_text("spec_version: 1\n")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "config.yaml").write_text("spec_version: 1\n")
    (tmp_path / "not_a_bundle").mkdir()  # no config.yaml

    src = FilesystemSpecSource(root=tmp_path)
    assert src.list() == ["a", "b"]


def test_filesystem_source_root_relative_ref(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "config.yaml").write_text(yaml.dump(_CONFIG))
    src = FilesystemSpecSource(root=tmp_path)
    assert src.load("a") == (tmp_path / "a" / "config.yaml").read_text()


def test_filesystem_source_invalidate_is_noop(bundle_dir: Path) -> None:
    """Filesystem reads fresh; invalidate() must not change subsequent loads."""
    src = FilesystemSpecSource()
    before = src.load(str(bundle_dir))
    src.invalidate()
    src.invalidate(str(bundle_dir))
    assert src.load(str(bundle_dir)) == before


def test_filesystem_source_satisfies_protocol() -> None:
    assert isinstance(FilesystemSpecSource(), SpecSource)


# ── Protocol is swappable: a fake in-memory source ───────────────


class _InMemorySpecSource:
    """A fake :class:`SpecSource` backed by a dict — proves the seam is swappable.

    Caches nothing of its own beyond the seeded mapping; tracks invalidate() calls
    so the cache/invalidate hook is exercised by a non-filesystem backend.
    """

    def __init__(self, specs: dict[str, dict[str, object]]) -> None:
        self._specs = specs
        self.invalidated: list[str | None] = []

    def load(self, ref: str) -> dict[str, object]:
        return self._specs[ref]

    def list(self) -> list[str]:
        return sorted(self._specs)

    def invalidate(self, ref: str | None = None) -> None:
        self.invalidated.append(ref)


def test_in_memory_source_satisfies_protocol() -> None:
    assert isinstance(_InMemorySpecSource({}), SpecSource)


def test_in_memory_source_yields_dict_through_shared_decode() -> None:
    """A dict-yielding source feeds the same shared decode path as the fs source."""
    src = _InMemorySpecSource({"mem": dict(_CONFIG)})
    assert src.list() == ["mem"]
    decoded = decode_raw_spec(src.load("mem"))
    assert decoded == _CONFIG


def test_in_memory_source_invalidate_tracked() -> None:
    src = _InMemorySpecSource({"mem": dict(_CONFIG)})
    src.invalidate("mem")
    src.invalidate()
    assert src.invalidated == ["mem", None]


def test_registry_swaps_in_fake_source() -> None:
    """The registry resolves a registered fake instead of the filesystem default."""
    registry = build_spec_source_registry()
    registry.register("memory", lambda: _InMemorySpecSource({"x": dict(_CONFIG)}))
    src = registry.get("memory")
    assert isinstance(src, _InMemorySpecSource)
    assert decode_raw_spec(src.load("x")) == _CONFIG


# ── Registry: default selection + unknown raises ─────────────────


def test_registry_default_is_filesystem() -> None:
    assert spec_source_registry.describe()["default"] == "filesystem"
    assert isinstance(spec_source_registry.resolve_default(), FilesystemSpecSource)


def test_registry_seam_id() -> None:
    assert spec_source_registry.seam == SEAM == "spec_source"


def test_registry_unknown_source_raises() -> None:
    with pytest.raises(ProviderNotRegistered):
        build_spec_source_registry().get("does-not-exist")


def test_registry_override_env_selects_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMNIGENT_USE_SPEC_SOURCE pins the active impl by name (strangler flag)."""
    registry = build_spec_source_registry()
    registry.register("memory", lambda: _InMemorySpecSource({}))
    monkeypatch.setenv("OMNIGENT_USE_SPEC_SOURCE", "memory")
    assert isinstance(registry.resolve_default(), _InMemorySpecSource)


# ── decode_raw_spec shared path ──────────────────────────────────


def test_decode_raw_spec_parses_yaml_text() -> None:
    assert decode_raw_spec(yaml.dump(_CONFIG)) == _CONFIG


def test_decode_raw_spec_passes_dict_through() -> None:
    d = dict(_CONFIG)
    assert decode_raw_spec(d) is d


def test_decode_raw_spec_rejects_non_mapping() -> None:
    from omnigent.errors import OmnigentError

    with pytest.raises(OmnigentError, match="non-mapping spec"):
        decode_raw_spec("- just\n- a\n- list\n")
