"""
Tests for :mod:`omnigent.resume_dispatch` — the top-level
``omnigent resume`` dispatcher.

The dispatcher's job is to translate the user's "take me back to
where I was" intent into the right wrapper call. The two important
properties under test are (a) we always preserve the Omnigent
conversation id end-to-end (no new id minted on resume) and (b)
claude-native conversations route to ``run_claude_native``,
everything else surfaces a clear redirect hint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import httpx
import pytest

from omnigent import resume_dispatch

# ── run_resume — top-level entry ──────────────────────────


def test_run_resume_picker_form_requires_server() -> None:
    """
    ``omnigent resume`` (no conv id, no --server) must fail loud.

    Without ``target`` we'd open the cross-agent picker; without
    ``--server`` we have no Omnigent endpoint to query. Starting an
    empty local server just for the picker would race with any
    other ``omnigent`` process the user has running, so we
    redirect via UsageError instead of silently doing it.
    """
    with pytest.raises(click.UsageError) as excinfo:
        resume_dispatch.run_resume(target=None, server=None)
    # Message names both ways out of the error: a conv id OR --server.
    assert "conv_" in str(excinfo.value)
    assert "--server" in str(excinfo.value)


def test_run_resume_picker_cancel_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Picker returns ``None`` (user pressed q / Enter on empty list)
    → dispatcher MUST return cleanly without calling
    ``run_claude_native``. A misroute that called the wrapper with
    ``session_id=None`` would silently create a fresh session the
    user explicitly chose not to create.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_pick_conversation_for_resume",
        lambda *, server: None,
    )
    invoked: list[str] = []

    def _fail_if_called(**kwargs: Any) -> None:
        """
        Marker for ``run_claude_native`` — fails the test if reached.

        :param kwargs: Wrapper kwargs (ignored).
        """
        del kwargs
        invoked.append("run_claude_native")

    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        _fail_if_called,
    )

    resume_dispatch.run_resume(
        target=None,
        server="https://example.com",
    )
    # If the wrapper was invoked we'd see "run_claude_native" here —
    # which would be the silent-fresh-session bug.
    assert invoked == []


# ── _dispatch_by_runtime — id-known dispatch ──────────────


def test_dispatch_by_runtime_claude_native_remote_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Remote claude-native conv ⇒ ``run_claude_native(server=..., session_id=conv_id)``.

    The Omnigent conv id MUST be preserved as ``session_id`` (the
    wrapper's resume kwarg). A bug that passed ``None`` would mint a
    fresh session and the user would lose their prior context.
    Also asserts ``server`` carries through so the wrapper hits the
    right Omnigent server.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_remote",
        lambda *, server, conv_id: "claude-code-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        """
        Record the kwargs ``run_claude_native`` was called with.

        :param kwargs: Wrapper kwargs.
        """
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.claude_native.run_claude_native", _capture)

    resume_dispatch._dispatch_by_runtime(
        target="conv_abc",
        server="https://example.com/",  # trailing slash — must be normalized
    )

    # session_id preserves the Omnigent conv id end-to-end.
    assert captured["session_id"] == "conv_abc"
    # Trailing slash stripped — the wrapper expects a bare base URL.
    assert captured["server"] == "https://example.com"
    # No leaking claude args; the wrapper builds its own.
    assert captured["claude_args"] == ()


def test_dispatch_by_runtime_codex_native_remote_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Remote codex-native conv ⇒ ``run_codex_native(server=..., session_id=conv_id)``.

    The Omnigent conv id must be preserved exactly like the
    claude-native path, but the runtime-specific passthrough kwarg is
    ``codex_args``.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_remote",
        lambda *, server, conv_id: "codex-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        """
        Record the kwargs ``run_codex_native`` was called with.

        :param kwargs: Wrapper kwargs.
        """
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.codex_native.run_codex_native", _capture)

    resume_dispatch._dispatch_by_runtime(
        target="conv_abc",
        server="https://example.com/",
    )

    assert captured["session_id"] == "conv_abc"
    assert captured["server"] == "https://example.com"
    assert captured["codex_args"] == ()


def test_dispatch_by_runtime_codex_native_local_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local codex-native conv routes to ``run_codex_native``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_local",
        lambda *, conv_id: "codex-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        """
        Record the kwargs ``run_codex_native`` was called with.

        :param kwargs: Wrapper kwargs.
        :returns: None.
        """
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.codex_native.run_codex_native", _capture)

    resume_dispatch._dispatch_by_runtime(
        target="conv_codex",
        server=None,
    )

    assert captured["session_id"] == "conv_codex"
    assert captured["server"] is None
    assert captured["codex_args"] == ()


def test_dispatch_by_runtime_claude_native_local_still_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local claude-native dispatch remains routed to ``run_claude_native``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_local",
        lambda *, conv_id: "claude-code-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        """
        Record the kwargs ``run_claude_native`` was called with.

        :param kwargs: Wrapper kwargs.
        :returns: None.
        """
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.claude_native.run_claude_native", _capture)

    resume_dispatch._dispatch_by_runtime(
        target="conv_claude",
        server=None,
    )

    assert captured["session_id"] == "conv_claude"
    assert captured["server"] is None
    assert captured["claude_args"] == ()


def test_dispatch_by_runtime_non_wrapper_local_raises_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local non-wrapper conv surfaces the ``omnigent run --resume`` hint.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_local",
        lambda *, conv_id: None,
    )

    with pytest.raises(click.ClickException) as excinfo:
        resume_dispatch._dispatch_by_runtime(
            target="conv_chat",
            server=None,
        )

    msg = excinfo.value.message
    assert "conv_chat" in msg
    assert "omnigent run --resume" in msg
    assert "<agent.yaml>" in msg


def test_read_wrapper_label_local_reads_persistent_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Local dispatch classifies sessions from ``~/.omnigent/chat.db``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary persistent Omnigent directory.
    :returns: None.
    """
    import omnigent.chat as chat_mod
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    db_path = tmp_path / "chat.db"
    store = SqlAlchemyConversationStore(f"sqlite:///{db_path}")
    created = store.create_session_with_agent(
        agent_id="ag_codex",
        agent_name="codex-native-ui",
        agent_bundle_location="ag_codex/bundle",
        agent_description=None,
        labels={"omnigent.wrapper": "codex-native-ui"},
    )
    monkeypatch.setattr(chat_mod, "_omnigent_persistent_dir", lambda: tmp_path)

    result = resume_dispatch._read_wrapper_label_local(conv_id=created.conversation.id)

    assert result == "codex-native-ui"


def test_dispatch_by_runtime_non_claude_native_remote_raises_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Remote non-claude-native conv ⇒ ``ClickException`` with a
    copy-pasteable ``omnigent run --resume`` hint.

    The hint MUST include both the conv id and the original
    ``--server`` URL so the user's next attempt works without
    them having to remember additional flags. A regression that
    surfaced a generic "wrong runtime" error would leave the
    user stuck.
    """
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_remote",
        lambda *, server, conv_id: None,  # no wrapper label
    )

    def _fail_if_called(**kwargs: Any) -> None:
        """Marker — fails the test if ``run_claude_native`` is called."""
        del kwargs
        raise AssertionError("run_claude_native invoked on non-claude conv")

    monkeypatch.setattr("omnigent.claude_native.run_claude_native", _fail_if_called)

    with pytest.raises(click.ClickException) as excinfo:
        resume_dispatch._dispatch_by_runtime(
            target="conv_xyz",
            server="https://example.com",
        )
    msg = excinfo.value.message
    # All three load-bearing pieces of the hint must appear.
    assert "conv_xyz" in msg
    assert "omnigent run --resume" in msg
    assert "https://example.com" in msg


# ── _read_wrapper_label_remote ────────────────────────────


def test_read_wrapper_label_remote_returns_label_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Happy path: 200 response with the wrapper label set returns the
    label value, which the caller compares against the claude-native
    sentinel.
    """

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        """
        Return a canned ``GET /v1/sessions/{id}`` response.

        :param url: Request URL (used to validate path shape).
        :param headers: Auth headers (ignored).
        :param timeout: Request timeout (ignored).
        :returns: A 200 response with a labelled body.
        """
        del headers, timeout
        assert url.endswith("/v1/sessions/conv_abc"), url
        return httpx.Response(
            200,
            json={
                "id": "conv_abc",
                "agent_id": "ag_1",
                "status": "idle",
                "created_at": 1,
                "labels": {"omnigent.wrapper": "claude-code-native-ui"},
            },
        )

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        lambda *, server_url: {},
    )

    result = resume_dispatch._read_wrapper_label_remote(
        server="https://example.com",
        conv_id="conv_abc",
    )
    assert result == "claude-code-native-ui"


def test_read_wrapper_label_remote_returns_none_when_label_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A conv with no ``omnigent.wrapper`` label returns ``None``, which
    the caller treats as "not claude-native" (the right call — wrappers
    stamp their label on every session they own; absence means a
    different runtime).
    """

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        """Return a 200 with no wrapper label."""
        del url, headers, timeout
        return httpx.Response(
            200,
            json={
                "id": "conv_abc",
                "agent_id": "ag_1",
                "status": "idle",
                "created_at": 1,
                "labels": {"some.other": "label"},
            },
        )

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        lambda *, server_url: {},
    )

    result = resume_dispatch._read_wrapper_label_remote(
        server="https://example.com",
        conv_id="conv_abc",
    )
    assert result is None


def test_read_wrapper_label_remote_raises_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    404 means the conv id doesn't exist — surface a clear error with
    the conv id and server so the user can fix a typo or check the
    server. Without this, the caller would proceed with a None label
    and surface the generic "not claude-native" hint, which would
    misdirect the user.
    """

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        """Return a 404."""
        del url, headers, timeout
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        lambda *, server_url: {},
    )

    with pytest.raises(click.ClickException) as excinfo:
        resume_dispatch._read_wrapper_label_remote(
            server="https://example.com",
            conv_id="conv_missing",
        )
    assert "conv_missing" in excinfo.value.message
    assert "not found" in excinfo.value.message


def test_run_resume_picker_selects_and_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Picker selection flows into runtime dispatch with the chosen id."""
    monkeypatch.setattr(
        resume_dispatch,
        "_pick_conversation_for_resume",
        lambda *, server: "conv_picked",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(resume_dispatch, "_dispatch_by_runtime", _capture)
    resume_dispatch.run_resume(target=None, server="https://example.com")
    assert captured == {"target": "conv_picked", "server": "https://example.com"}


def test_pick_conversation_for_resume_returns_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path wires OmnigentClient into the cross-agent picker."""

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def _fake_pick(_client: object) -> str:
        return "conv_from_picker"

    monkeypatch.setattr(
        "omnigent.repl._resume_picker.pick_conversation_cross_agent_from_sdk",
        _fake_pick,
    )
    monkeypatch.setattr(
        "omnigent_client.OmnigentClient",
        lambda **_kwargs: _Client(),
    )
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        lambda *, server_url: {"Authorization": "Bearer t"},
    )

    result = resume_dispatch._pick_conversation_for_resume(server="https://example.com/")
    assert result == "conv_from_picker"


def test_pick_conversation_for_resume_wraps_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network failures become actionable ClickException messages."""

    def _raise_http_error(_coro):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", "https://example.com"))

    monkeypatch.setattr(resume_dispatch.asyncio, "run", _raise_http_error)

    with pytest.raises(click.ClickException) as excinfo:
        resume_dispatch._pick_conversation_for_resume(server="https://example.com")
    assert "Failed to load conversations" in excinfo.value.message


def test_dispatch_by_runtime_pi_native_remote_routes_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote pi-native conv routes to ``run_pi_native``."""
    monkeypatch.setattr(
        resume_dispatch,
        "_read_wrapper_label_remote",
        lambda *, server, conv_id: "pi-native-ui",
    )
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.pi_native.run_pi_native", _capture)
    resume_dispatch._dispatch_by_runtime(target="conv_pi", server="https://example.com/")
    assert captured["session_id"] == "conv_pi"
    assert captured["server"] == "https://example.com"
    assert captured["pi_args"] == ()


def test_read_wrapper_label_local_raises_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unknown local conversation ids surface a clear store miss."""
    import omnigent.chat as chat_mod

    tmp_path / "chat.db"
    monkeypatch.setattr(chat_mod, "_omnigent_persistent_dir", lambda: tmp_path)

    with pytest.raises(click.ClickException) as excinfo:
        resume_dispatch._read_wrapper_label_local(conv_id="conv_missing")
    assert "conv_missing" in excinfo.value.message
    assert "not found" in excinfo.value.message


def test_read_wrapper_label_remote_raises_on_bad_status_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-200, invalid JSON, and non-object bodies fail loud."""

    def _resp_500(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        del url, headers, timeout
        return httpx.Response(500, text="upstream exploded")

    monkeypatch.setattr(httpx, "get", _resp_500)
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda *, server_url: {})
    with pytest.raises(click.ClickException, match="Failed to fetch"):
        resume_dispatch._read_wrapper_label_remote(server="https://example.com", conv_id="conv_x")

    def _resp_text(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        del url, headers, timeout
        return httpx.Response(200, text="not-json")

    monkeypatch.setattr(httpx, "get", _resp_text)
    with pytest.raises(click.ClickException, match="non-JSON"):
        resume_dispatch._read_wrapper_label_remote(server="https://example.com", conv_id="conv_x")

    def _resp_list(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        del url, headers, timeout
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "get", _resp_list)
    with pytest.raises(click.ClickException, match="non-object"):
        resume_dispatch._read_wrapper_label_remote(server="https://example.com", conv_id="conv_x")


def test_dispatch_wrapper_returns_false_for_unknown_wrapper() -> None:
    """Non-native wrapper labels are not dispatched in-process."""
    assert (
        resume_dispatch._dispatch_wrapper(
            wrapper="chat-ui",
            server=None,
            session_id="conv_chat",
        )
        is False
    )


def test_pick_conversation_for_resume_reraises_click_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickException from the picker is not double-wrapped."""

    def _raise_click(_coro):
        raise click.ClickException("picker cancelled")

    monkeypatch.setattr(resume_dispatch.asyncio, "run", _raise_click)
    with pytest.raises(click.ClickException, match="picker cancelled"):
        resume_dispatch._pick_conversation_for_resume(server="https://example.com")


def test_pick_conversation_for_resume_wraps_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected SDK failures become picker ClickExceptions."""

    def _raise_value(_coro):
        raise ValueError("sdk exploded")

    monkeypatch.setattr(resume_dispatch.asyncio, "run", _raise_value)
    with pytest.raises(click.ClickException, match="Picker failed"):
        resume_dispatch._pick_conversation_for_resume(server="https://example.com")


def test_read_wrapper_label_remote_raises_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport failures while fetching a remote session fail loud."""

    def _boom(*_args, **_kwargs):
        raise httpx.ConnectError("down", request=httpx.Request("GET", "https://example.com"))

    monkeypatch.setattr(httpx, "get", _boom)
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda *, server_url: {})
    with pytest.raises(click.ClickException, match="Failed to reach"):
        resume_dispatch._read_wrapper_label_remote(server="https://example.com", conv_id="conv_x")


def test_read_wrapper_label_remote_ignores_non_string_wrapper_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-string wrapper label values are treated as absent."""

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        del url, headers, timeout
        return httpx.Response(
            200,
            json={"labels": {"omnigent.wrapper": 42}},
        )

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda *, server_url: {})
    assert (
        resume_dispatch._read_wrapper_label_remote(
            server="https://example.com",
            conv_id="conv_x",
        )
        is None
    )
