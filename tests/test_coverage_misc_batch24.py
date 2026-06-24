"""Batch-24 coverage for runtime/chat/session modules and small gaps."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import click
import httpx
import pytest
from omnigent_client import SessionToolCallInfo, ToolCallInfo

import omnigent.chat as chat
import omnigent.conversation_browser as browser
from omnigent.claude_native_bridge import (
    _absolute_syntactic_path,
    _message_delta_from_jsonl_text,
    _trusted_parent_for_bridge_dir,
    read_bridge_id,
    read_launch_model,
    write_active_session_id,
)
from omnigent.coordination.replica_id import server_replica_id
from omnigent.errors import ErrorCode, OmnigentError, StaleWriteError
from omnigent.identity.types import Credential
from omnigent.runtime.backoff import ExpFullJitterBackoff
from omnigent.session_lifecycle import (
    CLOSED_LABEL_KEY,
    CLOSED_LABEL_VALUE,
    CLOSED_TITLE_INFIX,
    has_closed_title_marker,
    is_session_closed,
    labels_with_closed_status,
    title_without_closed_marker,
)


# ── omnigent/runtime/backoff.py ──────────────────────────────────────────────


def test_exp_full_jitter_applies_float_jitter_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jitter path multiplies the capped delay by ``uniform(0.5, 1.5)``."""
    monkeypatch.setattr("omnigent.runtime.backoff.random.uniform", lambda _a, _b: 1.25)
    assert ExpFullJitterBackoff().compute_delay(1, 2.0, 30.0) == 5.0


# ── omnigent/conversation_browser.py ─────────────────────────────────────────


def test_open_conversation_url_darwin_open_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS ``open`` returning non-zero yields ``False``."""
    import subprocess

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=["open"], returncode=1)

    monkeypatch.setattr(browser.sys, "platform", "darwin")
    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    assert browser.open_conversation_url("http://127.0.0.1:8000/c/conv") is False


# ── omnigent/session_lifecycle.py ────────────────────────────────────────────


def test_session_lifecycle_closed_marker_helpers() -> None:
    closed_title = f"agent:task{CLOSED_TITLE_INFIX}conv_abc"
    assert title_without_closed_marker(None) is None
    assert title_without_closed_marker(closed_title) == "agent:task"
    assert has_closed_title_marker(closed_title) is True
    assert is_session_closed({CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}) is True
    assert is_session_closed(None, closed_title) is True
    labels = labels_with_closed_status({"keep": "1"}, closed_title)
    assert labels["keep"] == "1"
    assert labels[CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE


# ── omnigent/errors.py ───────────────────────────────────────────────────────


def test_omnigent_error_init_and_unknown_http_status() -> None:
    err = OmnigentError("boom", code=ErrorCode.NOT_FOUND)
    assert err.message == "boom"
    assert err.code == ErrorCode.NOT_FOUND
    assert err.http_status == 404
    assert OmnigentError("x", code="totally_unknown").http_status == 500


def test_stale_write_error_init_sets_precondition_failed() -> None:
    err = StaleWriteError("stale etag")
    assert err.code == ErrorCode.PRECONDITION_FAILED
    assert str(err) == "stale etag"


# ── omnigent/identity/types.py ───────────────────────────────────────────────


def test_credential_header_property_default_and_custom() -> None:
    assert Credential("Bearer tok").header == {"Authorization": "Bearer tok"}
    assert Credential("secret", header_name="X-Key").header == {"X-Key": "secret"}


# ── omnigent/coordination/replica_id.py ──────────────────────────────────────


def test_server_replica_id_explicit_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_REPLICA_ID", "rid-42")
    assert server_replica_id() == "rid-42"


# ── omnigent/chat.py ─────────────────────────────────────────────────────────


def test_session_tool_adapter_delegates_to_legacy_handler() -> None:
    seen: list[ToolCallInfo] = []

    def execute(info: ToolCallInfo) -> str:
        seen.append(info)
        return "done"

    adapter = chat._SessionToolAdapter(
        tool_handler=SimpleNamespace(execute=execute),
        agent_name="polly",
    )
    result = adapter(
        SessionToolCallInfo(
            name="calc",
            arguments={"x": 1},
            call_id="call_1",
            item_id="item_1",
        )
    )
    assert result == "done"
    assert len(seen) == 1
    assert seen[0].name == "calc"
    assert seen[0].agent_name == "polly"
    assert seen[0].response_id == "item_1"


@pytest.mark.parametrize(
    ("target", "kwargs", "match"),
    [
        (
            "http://127.0.0.1:6767",
            {"server_url": "http://remote"},
            "--server is for binding a local agent YAML",
        ),
        (
            "http://127.0.0.1:6767",
            {"ephemeral": True},
            "--no-session / --continue / --resume / --log only apply",
        ),
        (
            "./agent",
            {"ephemeral": True, "server_url": "http://remote"},
            "--no-session is not supported with --server",
        ),
        (
            "http://127.0.0.1:6767",
            {"harness": "codex"},
            "--harness / --model / --system-prompt only apply to local",
        ),
    ],
)
def test_run_chat_validation_errors(
    target: str,
    kwargs: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(click.ClickException, match=match):
        chat.run_chat(target, None, **kwargs)  # type: ignore[arg-type]


def test_run_prompt_remote_url_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat, "_pick_agent", lambda _url, quiet=True: "agent-a")
    monkeypatch.setattr(chat, "_run_headless_prompt", lambda *a, **k: None)
    chat.run_prompt("http://127.0.0.1:6767/", None, prompt="hi")


def test_run_prompt_remote_rejects_local_overrides() -> None:
    with pytest.raises(click.ClickException, match="only apply to local"):
        chat.run_prompt(
            "http://127.0.0.1:6767",
            None,
            harness="codex",
            prompt="hi",
        )


def test_run_attach_fails_when_runner_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chat,
        "_attach_session_info",
        lambda **_kw: chat._AttachSessionInfo(
            runner_online=False,
            agent_name="polly",
            harness="codex",
        ),
    )
    with pytest.raises(click.ClickException, match="no online runner"):
        chat.run_attach(base_url="http://127.0.0.1:6767", conversation_id="conv_x")


def test_run_attach_dispatches_when_runner_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[object, ...]] = []

    monkeypatch.setattr(
        chat,
        "_attach_session_info",
        lambda **_kw: chat._AttachSessionInfo(
            runner_online=True,
            agent_name="polly",
            harness="codex",
        ),
    )

    def fake_chat_with_server(*args: object, **kwargs: object) -> None:
        seen.append((args, kwargs))

    monkeypatch.setattr(chat, "_chat_with_server", fake_chat_with_server)
    chat.run_attach(base_url="http://127.0.0.1:6767/", conversation_id="conv_abc")
    assert seen
    _args, kwargs = seen[0]
    assert kwargs["attach_only"] is True
    assert kwargs["attach_harness"] == "codex"


def test_remote_headers_oidc_and_databricks_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(chat._REMOTE_AUTH_TOKEN_ENV, raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: "oidc-jwt")
    assert chat._remote_headers("http://srv") == {"Authorization": "Bearer oidc-jwt"}

    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr(
        chat,
        "_stored_databricks_record_token",
        lambda _url: "dbx-token",
    )
    assert chat._remote_headers("http://srv") == {"Authorization": "Bearer dbx-token"}


def test_stored_databricks_record_token_returns_none_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.cli_auth.load_databricks_workspace_host",
        lambda _url: "https://workspace.example",
    )
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        lambda **_kw: (_ for _ in ()).throw(ValueError("no auth")),
    )
    assert chat._stored_databricks_record_token("http://app") is None


def test_databricks_token_auth_static_and_oidc_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(chat._REMOTE_AUTH_TOKEN_ENV, "static-token")
    auth = chat._DatabricksTokenAuth(server_url="http://srv")
    request = httpx.Request("GET", "http://srv/v1/sessions")
    gen = auth.auth_flow(request)
    out = next(gen)
    assert out.headers["Authorization"] == "Bearer static-token"

    monkeypatch.delenv(chat._REMOTE_AUTH_TOKEN_ENV, raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: "oidc")
    auth = chat._DatabricksTokenAuth(server_url="http://srv")
    request = httpx.Request("GET", "http://srv/v1/sessions")
    out = next(auth.auth_flow(request))
    assert out.headers["Authorization"] == "Bearer oidc"


def test_server_auth_returns_databricks_auth_when_login_record_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(chat._REMOTE_AUTH_TOKEN_ENV, raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr(
        "omnigent.cli_auth.load_databricks_workspace_host",
        lambda _url: "https://workspace.example",
    )
    assert isinstance(chat._server_auth("http://srv"), chat._DatabricksTokenAuth)


def test_redirect_native_resume_claude_codex_and_pi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        chat,
        "_wrapper_label_for_conversation",
        lambda **_kw: "claude-code-native-ui",
    )
    monkeypatch.setattr(
        chat,
        "_run_claude_native_resume_redirect",
        lambda **_kw: calls.append("claude"),
    )
    assert (
        chat._redirect_native_resume_if_needed(
            base_url="http://srv",
            conversation_id="conv_1",
            auto_open_conversation=False,
        )
        is True
    )
    assert calls == ["claude"]

    calls.clear()
    monkeypatch.setattr(
        chat,
        "_wrapper_label_for_conversation",
        lambda **_kw: "codex-native-ui",
    )
    monkeypatch.setattr(
        chat,
        "_run_codex_native_resume_redirect",
        lambda **_kw: calls.append("codex"),
    )
    assert chat._redirect_native_resume_if_needed(
        base_url="http://srv",
        conversation_id="conv_1",
        auto_open_conversation=False,
    )
    assert calls == ["codex"]

    calls.clear()
    monkeypatch.setattr(
        chat,
        "_wrapper_label_for_conversation",
        lambda **_kw: "pi-native-ui",
    )
    monkeypatch.setattr(
        chat,
        "_run_pi_native_resume_redirect",
        lambda **_kw: calls.append("pi"),
    )
    assert chat._redirect_native_resume_if_needed(
        base_url="http://srv",
        conversation_id="conv_1",
        auto_open_conversation=False,
    )
    assert calls == ["pi"]


def test_finish_native_redirect_progress_finishes_spinner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    progress = SimpleNamespace(finish=MagicMock())
    chat._finish_native_redirect_progress(
        progress=progress,
        conversation_id="conv_abc",
        wrapper_name="codex-native",
        native_command="codex",
    )
    progress.finish.assert_called_once()
    assert "conv_abc" in capsys.readouterr().err


def test_wrapper_label_for_conversation_handles_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat, "_remote_headers", lambda **_kw: {})
    monkeypatch.setattr(
        chat.httpx,
        "get",
        lambda *_a, **_kw: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    assert chat._wrapper_label_for_conversation(
        base_url="http://srv",
        conversation_id="conv_x",
    ) is None

    monkeypatch.setattr(
        chat.httpx,
        "get",
        lambda *_a, **_kw: SimpleNamespace(status_code=404, json=lambda: {}),
    )
    assert chat._wrapper_label_for_conversation(
        base_url="http://srv",
        conversation_id="conv_x",
    ) is None

    monkeypatch.setattr(
        chat.httpx,
        "get",
        lambda *_a, **_kw: SimpleNamespace(
            status_code=200,
            json=lambda: {"labels": {"omnigent.wrapper": "codex-native-ui"}},
        ),
    )
    assert (
        chat._wrapper_label_for_conversation(
            base_url="http://srv",
            conversation_id="conv_x",
        )
        == "codex-native-ui"
    )


def test_attach_session_info_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "_remote_headers", lambda **_kw: {})

    monkeypatch.setattr(
        chat.httpx,
        "get",
        lambda *_a, **_kw: SimpleNamespace(
            status_code=200,
            json=lambda: {
                "runner_id": "runner_1",
                "runner_online": True,
                "agent_name": "polly",
                "harness": "codex",
            },
        ),
    )
    info = chat._attach_session_info(base_url="http://srv", conversation_id="conv_1")
    assert info.runner_online is True
    assert info.agent_name == "polly"
    assert info.harness == "codex"

    monkeypatch.setattr(
        chat.httpx,
        "get",
        lambda *_a, **_kw: SimpleNamespace(
            status_code=200,
            json=lambda: {"runner_id": "runner_1"},
        ),
    )
    info = chat._attach_session_info(base_url="http://srv", conversation_id="conv_1")
    assert info.runner_online is True


def test_pick_agent_auto_selects_single_name(monkeypatch: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"data": [{"agent_name": "only-one"}]},
    )
    monkeypatch.setattr(chat, "_remote_headers", lambda **_kw: {})
    monkeypatch.setattr(chat.httpx, "get", lambda *_a, **_kw: response)
    assert chat._pick_agent("http://srv", quiet=True) == "only-one"


def test_load_tool_handler_modern_and_legacy_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modern = SimpleNamespace(_TOOL_FNS=[lambda: None])
    monkeypatch.setattr("omnigent.client_tools.get_tool_set", lambda _name: modern)
    with patch("omnigent_client.tools.build_tool_handler", return_value="modern") as build:
        assert chat._load_tool_handler("coding") == "modern"
    build.assert_called_once()

    legacy = SimpleNamespace(
        TOOLS={"x": {}},
        execute_tool=lambda _name, _args: "legacy-out",
    )
    monkeypatch.setattr("omnigent.client_tools.get_tool_set", lambda _name: legacy)
    handler = chat._load_tool_handler("coding")
    assert handler.execute(
        ToolCallInfo(
            name="x",
            arguments={},
            call_id="c",
            agent_name="a",
            response_id="r",
            iteration=0,
        )
    ) == "legacy-out"


def test_load_tool_handler_missing_set_raises() -> None:
    with patch(
        "omnigent.client_tools.get_tool_set",
        side_effect=SystemExit(2),
    ):
        with pytest.raises(click.ClickException, match="not found"):
            chat._load_tool_handler("missing")


def test_is_claude_native_conversation_checks_wrapper_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat,
        "_wrapper_label_for_conversation",
        lambda **_kw: chat._CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
    )
    assert (
        chat._is_claude_native_conversation(
            base_url="http://srv",
            conversation_id="conv_1",
        )
        is True
    )


@pytest.mark.parametrize(
    ("redirect_fn", "runner_attr", "wrapper_name"),
    [
        (chat._run_claude_native_resume_redirect, "run_claude_native", "claude"),
        (chat._run_codex_native_resume_redirect, "run_codex_native", "codex"),
        (chat._run_pi_native_resume_redirect, "run_pi_native", "pi"),
    ],
)
def test_native_resume_redirect_invokes_wrapper_cli(
    monkeypatch: pytest.MonkeyPatch,
    redirect_fn: object,
    runner_attr: str,
    wrapper_name: str,
) -> None:
    seen: list[dict[str, object]] = []

    def capture(**kwargs: object) -> None:
        seen.append(kwargs)

    if wrapper_name == "claude":
        monkeypatch.setattr("omnigent.claude_native.run_claude_native", capture)
    elif wrapper_name == "codex":
        monkeypatch.setattr("omnigent.codex_native.run_codex_native", capture)
    else:
        monkeypatch.setattr("omnigent.pi_native.run_pi_native", capture)

    redirect_fn(  # type: ignore[operator]
        base_url="http://srv",
        conversation_id="conv_abc",
        auto_open_conversation=True,
        progress=None,
    )
    assert seen == [
        {
            "server": "http://srv",
            "session_id": "conv_abc",
            f"{wrapper_name}_args": (),
            "auto_open_conversation": True,
        }
    ]


def test_run_chat_ephemeral_local_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "config.yaml").write_text("name: demo\nprompt: hi\n", encoding="utf-8")
    seen: list[bool] = []

    monkeypatch.setattr(chat, "_chat_local", lambda *a, **k: seen.append(k.get("ephemeral", False)))
    chat.run_chat(str(agent), None, ephemeral=True)
    assert seen == [True]


# ── omnigent/claude_native_bridge.py ─────────────────────────────────────────


def test_absolute_syntactic_path_expands_user() -> None:
    path = _absolute_syntactic_path(Path("~/bridge-dir"))
    assert path.is_absolute()


def test_trusted_parent_for_claude_and_codex_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    claude_dir = tmp_path / "claude-native" / "bridge-1"
    monkeypatch.setattr(
        "omnigent.claude_native_bridge._BRIDGE_ROOT",
        tmp_path / "claude-native",
    )
    monkeypatch.setattr(
        "omnigent.claude_native_bridge._TRUSTED_PARENT",
        tmp_path,
    )
    assert _trusted_parent_for_bridge_dir(claude_dir) == tmp_path.resolve()

    codex_root = tmp_path / ".omnigent" / "codex-native"
    codex_dir = codex_root / "bridge-2"
    monkeypatch.setattr(
        "omnigent.codex_native_bridge.bridge_root",
        lambda: codex_root,
    )
    assert _trusted_parent_for_bridge_dir(codex_dir) == tmp_path.resolve()


def test_trusted_parent_rejects_unknown_root(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not under an allowed bridge root"):
        _trusted_parent_for_bridge_dir(tmp_path / "elsewhere")


@pytest.mark.parametrize(
    ("text", "expected_message_id"),
    [
        (None, None),
        ("", None),
        ("not-json", None),
        ('{"message_id":"m1","delta":"hi","index":0}', "m1"),
        ('{"message_id":"m1","delta":"hi","index":true}', None),
    ],
)
def test_message_delta_from_jsonl_text_edges(
    text: str | None,
    expected_message_id: str | None,
) -> None:
    delta = _message_delta_from_jsonl_text(text)
    if expected_message_id is None:
        assert delta is None
    else:
        assert delta is not None
        assert delta.message_id == expected_message_id


def test_read_launch_model_and_bridge_id(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    config = bridge_dir / "bridge.json"
    config.write_text(
        json.dumps(
            {
                "launch_model": "databricks-claude-opus-4-7",
                "bridge_id": "bridge_abc",
            }
        ),
        encoding="utf-8",
    )
    assert read_launch_model(bridge_dir) == "databricks-claude-opus-4-7"
    assert read_bridge_id(bridge_dir) == "bridge_abc"
    assert read_launch_model(tmp_path / "missing") is None


def test_write_active_session_id_requires_existing_config(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    with pytest.raises(RuntimeError, match="bridge config missing"):
        write_active_session_id(bridge_dir, "conv_new")