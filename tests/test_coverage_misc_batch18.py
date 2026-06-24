"""Batch-18 coverage for small omnigent modules with 1–3 statement gaps."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.coordination.replica_id import server_replica_id
from omnigent.grok_native import _materialize_grok_agent_spec
from omnigent.harness_aliases import is_claude_sdk_harness_name
from omnigent.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE, labels_with_closed_status


def test_is_claude_sdk_harness_name_accepts_alias_and_canonical() -> None:
    """``is_claude_sdk_harness_name`` is true for the SDK harness and shorthand."""
    assert is_claude_sdk_harness_name("claude-sdk") is True
    assert is_claude_sdk_harness_name("claude") is True
    assert is_claude_sdk_harness_name("claude-native") is False
    assert is_claude_sdk_harness_name(None) is False


def test_server_replica_id_prefers_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNIGENT_REPLICA_ID`` wins over hostname discovery."""
    monkeypatch.setenv("OMNIGENT_REPLICA_ID", "replica-explicit")
    monkeypatch.setenv("HOSTNAME", "pod-ignored")
    assert server_replica_id() == "replica-explicit"


def test_server_replica_id_uses_hostname_when_not_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kubernetes-style hostnames become the stable replica id."""
    monkeypatch.delenv("OMNIGENT_REPLICA_ID", raising=False)
    monkeypatch.setenv("HOSTNAME", "omnigent-server-7f3c")
    assert server_replica_id() == "omnigent-server-7f3c"


def test_server_replica_id_falls_back_to_local_suffix_on_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local dev without a real hostname gets a short random replica id."""
    monkeypatch.delenv("OMNIGENT_REPLICA_ID", raising=False)
    monkeypatch.setenv("HOSTNAME", "localhost")
    rid = server_replica_id()
    assert rid.startswith("local-")
    assert len(rid) == len("local-") + 12


def test_materialize_grok_agent_spec_includes_model_override(tmp_path: Path) -> None:
    """Optional model id is written into the generated executor block."""
    path = _materialize_grok_agent_spec(tmp_path, model="grok-build")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["executor"]["model"] == "grok-build"
    assert data["executor"]["harness"] == "grok-native"


def test_open_conversation_url_uses_webbrowser_on_non_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-macOS platforms delegate to :func:`webbrowser.open`."""
    import omnigent.conversation_browser as browser

    opened: list[str] = []

    def _fake_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.setattr(browser.webbrowser, "open", _fake_open)

    assert browser.open_conversation_url("http://127.0.0.1:8000/c/conv_abc") is True
    assert opened == ["http://127.0.0.1:8000/c/conv_abc"]


def test_labels_with_closed_status_adds_label_from_legacy_title() -> None:
    """Legacy title suffixes synthesize ``omnigent.closed=true`` on read."""
    labels = labels_with_closed_status(
        {"omnigent.wrapper": "codex-native-ui"},
        "researcher:auth:closed:conv_abc123",
    )
    assert labels["omnigent.wrapper"] == "codex-native-ui"
    assert labels[CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE