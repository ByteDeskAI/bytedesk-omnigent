"""Batch-23 coverage for small server/runtime modules."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bytedesk_omnigent.harnesses.config_apply import apply_spec_to_hermes
from omnigent._e2e_policy_callables import block_on_sentinel, taint_on_banana
from omnigent.host import _daemon_entry
from omnigent.policies.types import PolicyResult
from omnigent.server.passwords import (
    InvalidPasswordError,
    hash_password,
    needs_rehash,
    verify_password,
)
from omnigent.spec.types import PolicyAction


# ── omnigent/server/passwords.py ─────────────────────────────────────────────


def test_invalid_password_error_is_distinct_exception() -> None:
    assert issubclass(InvalidPasswordError, Exception)


def test_password_lifecycle_hash_verify_and_rehash() -> None:
    digest = hash_password("batch23-secret")
    verify_password("batch23-secret", digest)
    assert needs_rehash(digest) is False
    with pytest.raises(InvalidPasswordError):
        verify_password("wrong-secret", digest)


def test_verify_password_collapses_non_mismatch_errors() -> None:
    with pytest.raises(InvalidPasswordError):
        verify_password("anything", "not-a-real-argon2-hash")


# ── omnigent/host/_daemon_entry.py ───────────────────────────────────────────


def test_daemon_entry_main_connects_to_explicit_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def _fake_run_host_process(*, server_url: str) -> None:
        seen.append(server_url)

    monkeypatch.setattr(
        "omnigent.host.connect.run_host_process",
        _fake_run_host_process,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["omnigent.host._daemon_entry", "--server", "http://127.0.0.1:9000"],
    )
    _daemon_entry.main()
    assert seen == ["http://127.0.0.1:9000"]


def test_daemon_entry_main_local_mode_starts_local_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def _fake_ensure() -> SimpleNamespace:
        return SimpleNamespace(url="http://127.0.0.1:8000")

    def _fake_run_host_process(*, server_url: str) -> None:
        seen.append(server_url)

    monkeypatch.setattr(
        "omnigent.host.local_server.ensure_local_omnigent_server",
        _fake_ensure,
    )
    monkeypatch.setattr(
        "omnigent.host.connect.run_host_process",
        _fake_run_host_process,
    )
    monkeypatch.setattr(sys, "argv", ["omnigent.host._daemon_entry", "--local"])
    _daemon_entry.main()
    assert seen == ["http://127.0.0.1:8000"]


@pytest.mark.parametrize(
    "argv",
    [
        ["omnigent.host._daemon_entry"],
        ["omnigent.host._daemon_entry", "--local", "--server", "http://x"],
    ],
)
def test_daemon_entry_main_rejects_ambiguous_mode(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        with patch.object(sys, "argv", argv):
            _daemon_entry.main()


def test_daemon_entry_module_main_invokes_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def _fake_run_host_process(*, server_url: str) -> None:
        seen.append(server_url)

    monkeypatch.setattr(
        "omnigent.host.connect.run_host_process",
        _fake_run_host_process,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["omnigent.host._daemon_entry", "--server", "http://127.0.0.1:9000"],
    )
    runpy.run_module("omnigent.host._daemon_entry", run_name="__main__")
    assert seen == ["http://127.0.0.1:9000"]


# ── omnigent/_e2e_policy_callables.py ────────────────────────────────────────


def test_block_on_sentinel_denies_when_token_present() -> None:
    decision = block_on_sentinel({"data": "please BLOCK_THIS_TOKEN now"})
    assert decision["result"] == "DENY"
    assert "BLOCK_THIS_TOKEN" in decision["reason"]


def test_block_on_sentinel_allows_clean_input() -> None:
    assert block_on_sentinel({"data": "ordinary user text"}) == {"result": "ALLOW"}


def test_block_on_sentinel_allows_non_string_data() -> None:
    assert block_on_sentinel({"data": {"text": "BLOCK_THIS_TOKEN"}}) == {"result": "ALLOW"}


def test_taint_on_banana_sets_label_when_trigger_present() -> None:
    result = taint_on_banana({"data": "hello BANANA_TRIGGER world"})
    assert isinstance(result, PolicyResult)
    assert result.action == PolicyAction.ALLOW
    assert result.set_labels == {"tainted": "1"}


def test_taint_on_banana_allows_without_trigger() -> None:
    result = taint_on_banana({"data": "no trigger here"})
    assert isinstance(result, PolicyResult)
    assert result.action == PolicyAction.ALLOW
    assert result.set_labels is None


def test_taint_on_banana_allows_non_string_data() -> None:
    result = taint_on_banana({"data": ["BANANA_TRIGGER"]})
    assert result.action == PolicyAction.ALLOW
    assert result.set_labels is None


# ── bytedesk_omnigent/harnesses/config_apply.py ──────────────────────────────


def test_apply_spec_to_hermes_defaults_home_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "bytedesk_omnigent.harnesses.config_apply.Path.home",
        lambda: tmp_path,
    )
    changed = apply_spec_to_hermes("default-home persona")
    assert changed is True
    assert (tmp_path / ".hermes" / "SOUL.md").read_text(encoding="utf-8") == "default-home persona"


def test_apply_spec_to_hermes_default_home_noop_on_repeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "bytedesk_omnigent.harnesses.config_apply.Path.home",
        lambda: tmp_path,
    )
    prompt = "stable default-home persona"
    assert apply_spec_to_hermes(prompt) is True
    assert apply_spec_to_hermes(prompt) is False