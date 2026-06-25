"""Batch-44 coverage for env compat, client_tools registry, and coordination factory."""

from __future__ import annotations

import os
import sys

import pytest

from omnigent._env_compat import mirror_legacy_env
from omnigent.client_tools import get_tool_set
from omnigent.coordination.factory import get_coordination_registry


def test_mirror_legacy_env_maps_omnigents_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_SKIP_WEB_UI", raising=False)
    monkeypatch.setenv("OMNIGENTS_SKIP_WEB_UI", "1")
    mirror_legacy_env.__globals__["_mirrored"] = False
    mirror_legacy_env()
    assert os.environ["OMNIGENT_SKIP_WEB_UI"] == "1"


def test_mirror_legacy_env_prefers_explicit_omnigent_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTS_SKIP_WEB_UI", "legacy")
    monkeypatch.setenv("OMNIGENT_SKIP_WEB_UI", "current")
    mirror_legacy_env.__globals__["_mirrored"] = False
    mirror_legacy_env()
    assert os.environ["OMNIGENT_SKIP_WEB_UI"] == "current"


def test_mirror_legacy_env_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_FOO", raising=False)
    monkeypatch.setenv("OMNIAGENTS_FOO", "first")
    mirror_legacy_env.__globals__["_mirrored"] = False
    mirror_legacy_env()
    assert os.environ["OMNIGENT_FOO"] == "first"
    os.environ["OMNIGENT_FOO"] = "overwritten"
    mirror_legacy_env()
    assert os.environ["OMNIGENT_FOO"] == "overwritten"


def test_get_tool_set_unknown_name_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as exc:
        get_tool_set("does-not-exist")
    assert exc.value.code == 1


def test_nats_factory_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_NATS_URL", raising=False)
    factory = get_coordination_registry()._factories["nats"]  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="OMNIGENT_NATS_URL is required"):
        factory()
