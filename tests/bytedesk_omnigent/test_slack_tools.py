"""Tests for the native ``bytedesk_slack`` agent tool (BDP-2405).

The HTTP layer is mocked with ``httpx.MockTransport`` — no real network. The
adapter takes credentials directly (bypassing the secret backend) except in the
dedicated "not configured" tests, which monkeypatch ``load_secret``.

Mirrors ``test_github_tools.py`` (BDP-2404) — same shape, same error contract,
with one Slack-specific twist: the Slack Web API always answers HTTP 200 with a
``{"ok": ...}`` envelope, so a Slack ``ok:false`` (e.g. ``channel_not_found``) is
passed through verbatim as the canonical surfaced error, and only a non-200 /
transport blip becomes ``slack_request_failed``. Read + post only: no delete or
admin operations exist.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from bytedesk_omnigent.tools.slack_tools import (
    BytedeskSlackTool,
    _SlackClient,
)
from omnigent.tools.base import ToolContext

_BASE = "https://slack.com/api"
_TOKEN = "xoxb-test-token"
_CHANNEL = "C0DEFAULT"

_CTX = ToolContext(task_id="t1", agent_id="ag_1")


def _make_tool(
    handler, *, default_channel: str | None = _CHANNEL
) -> tuple[BytedeskSlackTool, list[httpx.Request]]:
    """Build a tool whose adapter routes every request through ``handler``.

    Returns the tool and a list that captures each issued request for assertions.
    """
    captured: list[httpx.Request] = []

    def _capturing(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_capturing)
    client = httpx.Client(base_url=_BASE, transport=transport)
    adapter = _SlackClient(
        base_url=_BASE,
        token=_TOKEN,
        default_channel=default_channel,
        client=client,
    )
    return BytedeskSlackTool(client=adapter), captured


def _call(tool: BytedeskSlackTool, **args: Any) -> dict[str, Any]:
    return json.loads(tool.invoke(json.dumps(args), _CTX))


def _ok(**extra: Any) -> dict[str, Any]:
    return {"ok": True, **extra}


# ── auth / headers ──────────────────────────────────────────────────────────────


def test_get_requests_send_bearer_token():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok(members=[])))
    _call(tool, op="list_users")

    req = captured[0]
    assert req.headers["Authorization"] == f"Bearer {_TOKEN}"


def test_post_requests_send_bearer_token_and_json_content_type():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok(ts="1.2")))
    _call(tool, op="post_message", text="hi")

    req = captured[0]
    assert req.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert req.headers["Content-Type"] == "application/json; charset=utf-8"


def test_token_value_is_not_echoed_in_results():
    tool, _ = _make_tool(lambda r: httpx.Response(200, json=_ok(ts="1.2")))
    raw = tool.invoke(json.dumps({"op": "post_message", "text": "hi"}), _CTX)
    assert _TOKEN not in raw


# ── post_message ─────────────────────────────────────────────────────────────────


def test_post_message_posts_channel_and_text():
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json=_ok(ts="1700000000.001"))
    )

    result = _call(tool, op="post_message", text="hello team")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/chat.postMessage"
    sent = json.loads(req.content)
    assert sent == {"channel": _CHANNEL, "text": "hello team"}
    assert result["ok"] is True
    assert result["ts"] == "1700000000.001"


def test_post_message_includes_thread_ts_when_present():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok(ts="1.2")))

    _call(tool, op="post_message", text="reply", thread_ts="1699999999.000")

    sent = json.loads(captured[0].content)
    assert sent == {
        "channel": _CHANNEL,
        "text": "reply",
        "thread_ts": "1699999999.000",
    }


def test_post_message_omits_thread_ts_when_absent():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok(ts="1.2")))
    _call(tool, op="post_message", text="top-level")
    assert "thread_ts" not in json.loads(captured[0].content)


def test_post_message_honors_channel_override():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok(ts="1.2")))
    _call(tool, op="post_message", text="x", channel="C0OTHER")
    assert json.loads(captured[0].content)["channel"] == "C0OTHER"


def test_post_message_requires_text():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok()))
    result = _call(tool, op="post_message")
    assert result["ok"] is False
    assert "text" in result["error"]
    assert captured == []


# ── list_channels ────────────────────────────────────────────────────────────────


def test_list_channels_passes_types_and_limit():
    body = _ok(channels=[{"id": "C1", "name": "general"}])
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(
        tool, op="list_channels", types="public_channel,private_channel", limit=25
    )

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/conversations.list"
    assert req.url.params["types"] == "public_channel,private_channel"
    assert req.url.params["limit"] == "25"
    assert result["ok"] is True
    assert result["channels"][0]["name"] == "general"


def test_list_channels_defaults_types_and_limit():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok(channels=[])))
    _call(tool, op="list_channels")
    assert captured[0].url.params["types"] == "public_channel"
    assert captured[0].url.params["limit"] == "100"


# ── channel_history ──────────────────────────────────────────────────────────────


def test_channel_history_passes_channel_and_limit():
    body = _ok(messages=[{"ts": "1.0", "text": "first"}])
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="channel_history", limit=10)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/conversations.history"
    assert req.url.params["channel"] == _CHANNEL
    assert req.url.params["limit"] == "10"
    assert result["ok"] is True
    assert result["messages"][0]["text"] == "first"


def test_channel_history_defaults_limit_50():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok(messages=[])))
    _call(tool, op="channel_history")
    assert captured[0].url.params["limit"] == "50"


def test_channel_history_honors_channel_override():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok(messages=[])))
    _call(tool, op="channel_history", channel="C0OTHER")
    assert captured[0].url.params["channel"] == "C0OTHER"


# ── get_thread ───────────────────────────────────────────────────────────────────


def test_get_thread_passes_channel_and_ts():
    body = _ok(messages=[{"ts": "1.0"}, {"ts": "1.1"}])
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="get_thread", ts="1700000000.000")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/conversations.replies"
    assert req.url.params["channel"] == _CHANNEL
    assert req.url.params["ts"] == "1700000000.000"
    assert result["ok"] is True
    assert len(result["messages"]) == 2


def test_get_thread_requires_ts():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok()))
    result = _call(tool, op="get_thread")
    assert result["ok"] is False
    assert "ts" in result["error"]
    assert captured == []


# ── list_users ───────────────────────────────────────────────────────────────────


def test_list_users_passes_limit():
    body = _ok(members=[{"id": "U1", "name": "alice"}])
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="list_users", limit=42)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/users.list"
    assert req.url.params["limit"] == "42"
    assert result["ok"] is True
    assert result["members"][0]["name"] == "alice"


def test_list_users_defaults_limit_100():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok(members=[])))
    _call(tool, op="list_users")
    assert captured[0].url.params["limit"] == "100"


# ── user_info ────────────────────────────────────────────────────────────────────


def test_user_info_passes_user():
    body = _ok(user={"id": "U1", "name": "alice"})
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="user_info", user="U1")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/users.info"
    assert req.url.params["user"] == "U1"
    assert result["ok"] is True
    assert result["user"]["name"] == "alice"


def test_user_info_requires_user():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok()))
    result = _call(tool, op="user_info")
    assert result["ok"] is False
    assert "user" in result["error"]
    assert captured == []


# ── add_reaction ─────────────────────────────────────────────────────────────────


def test_add_reaction_posts_timestamp_and_name():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok()))

    result = _call(tool, op="add_reaction", ts="1700000000.000", name="thumbsup")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/reactions.add"
    sent = json.loads(req.content)
    assert sent == {
        "channel": _CHANNEL,
        "timestamp": "1700000000.000",
        "name": "thumbsup",
    }
    assert result["ok"] is True


def test_add_reaction_honors_channel_override():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok()))
    _call(tool, op="add_reaction", ts="1.0", name="eyes", channel="C0OTHER")
    assert json.loads(captured[0].content)["channel"] == "C0OTHER"


def test_add_reaction_requires_ts_and_name():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok()))
    assert _call(tool, op="add_reaction", ts="1.0")["ok"] is False
    assert _call(tool, op="add_reaction", name="eyes")["ok"] is False
    assert captured == []


# ── channel configuration ────────────────────────────────────────────────────────


def test_missing_channel_returns_slack_channel_not_configured():
    # no channel arg AND no default channel → structured error, no network
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json=_ok(ts="1.2")), default_channel=None
    )
    result = _call(tool, op="post_message", text="hi")
    assert result == {"ok": False, "error": "slack_channel_not_configured"}
    assert captured == []


def test_channel_arg_works_without_default_channel():
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json=_ok(ts="1.2")), default_channel=None
    )
    result = _call(tool, op="post_message", text="hi", channel="C0XYZ")
    assert result["ok"] is True
    assert json.loads(captured[0].content)["channel"] == "C0XYZ"


def test_list_channels_does_not_need_channel():
    # channel-free ops must not fire the missing-channel guard even with no default.
    tool, captured = _make_tool(
        lambda r: httpx.Response(200, json=_ok(channels=[])), default_channel=None
    )
    result = _call(tool, op="list_channels")
    assert result["ok"] is True
    assert captured[0].url.path == "/api/conversations.list"


# ── Slack ok:false envelope passthrough ──────────────────────────────────────────


def test_slack_ok_false_is_passed_through_not_translated():
    # Slack answers 200 with {"ok": false, "error": "channel_not_found"} — that IS
    # the canonical surfaced error and must pass through verbatim.
    body = {"ok": False, "error": "channel_not_found"}
    tool, _ = _make_tool(lambda r: httpx.Response(200, json=body))

    result = _call(tool, op="post_message", text="hi", channel="C0BAD")
    assert result == {"ok": False, "error": "channel_not_found"}


def test_slack_ok_false_passthrough_on_read_op():
    body = {"ok": False, "error": "not_in_channel"}
    tool, _ = _make_tool(lambda r: httpx.Response(200, json=body))
    result = _call(tool, op="channel_history", channel="C0BAD")
    assert result == {"ok": False, "error": "not_in_channel"}


# ── graceful errors ──────────────────────────────────────────────────────────────


def test_unknown_op_returns_structured_error():
    tool, captured = _make_tool(lambda r: httpx.Response(200, json=_ok()))
    result = _call(tool, op="frobnicate")
    assert result["ok"] is False
    assert "unknown op" in result["error"]
    assert captured == []


def test_http_500_returns_request_failed_not_raised():
    tool, _ = _make_tool(lambda r: httpx.Response(500, text="boom"))
    result = _call(tool, op="list_users")
    assert result["ok"] is False
    assert result["error"] == "slack_request_failed"


def test_http_429_returns_request_failed_not_raised():
    tool, _ = _make_tool(lambda r: httpx.Response(429, text="slow down"))
    result = _call(tool, op="post_message", text="hi")
    assert result["ok"] is False
    assert result["error"] == "slack_request_failed"


def test_transport_error_returns_request_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    tool, _ = _make_tool(handler)
    result = _call(tool, op="list_users")
    assert result["ok"] is False
    assert result["error"] == "slack_request_failed"


def test_missing_credentials_returns_slack_not_configured(monkeypatch):
    import omnigent.onboarding.secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: None)

    tool = BytedeskSlackTool()  # default adapter → secret backend
    result = _call(tool, op="post_message", text="hi", channel="C0XYZ")
    assert result == {"ok": False, "error": "slack_not_configured"}


def test_invalid_arguments_json_is_graceful():
    tool, _ = _make_tool(lambda r: httpx.Response(200, json=_ok()))
    result = json.loads(tool.invoke("{not json", _CTX))
    assert result["ok"] is False
    assert result["error"] == "invalid_arguments_json"


# ── credential resolution + fallbacks ───────────────────────────────────────────


def test_token_falls_back_to_bytedesk_slack_token(monkeypatch):
    import omnigent.onboarding.secrets as secrets_mod

    values = {
        "BYTEDESK_SLACK_TOKEN": "fallback-tok",
        "SLACK_DEFAULT_CHANNEL": "C0SEED",
    }
    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: values.get(name))

    adapter = _SlackClient()
    adapter._resolve_credentials()
    assert adapter._token == "fallback-tok"
    assert adapter._default_channel == "C0SEED"


def test_token_prefers_slack_bot_token_over_fallback(monkeypatch):
    import omnigent.onboarding.secrets as secrets_mod

    values = {
        "SLACK_BOT_TOKEN": "primary-tok",
        "BYTEDESK_SLACK_TOKEN": "fallback-tok",
    }
    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: values.get(name))

    adapter = _SlackClient()
    adapter._resolve_credentials()
    assert adapter._token == "primary-tok"


def test_default_channel_optional_resolves_to_none(monkeypatch):
    import omnigent.onboarding.secrets as secrets_mod

    values = {"SLACK_BOT_TOKEN": "tok"}  # no default channel configured
    monkeypatch.setattr(secrets_mod, "load_secret", lambda name: values.get(name))

    adapter = _SlackClient()
    adapter._resolve_credentials()
    assert adapter._token == "tok"
    assert adapter._default_channel is None


# ── tool surface ─────────────────────────────────────────────────────────────────


def test_tool_name_and_schema():
    assert BytedeskSlackTool.name() == "bytedesk_slack"
    schema = BytedeskSlackTool().get_schema()
    assert schema["function"]["name"] == "bytedesk_slack"
    assert schema["function"]["parameters"]["required"] == ["op"]
    ops = schema["function"]["parameters"]["properties"]["op"]["enum"]
    assert set(ops) == {
        "post_message",
        "list_channels",
        "channel_history",
        "get_thread",
        "list_users",
        "user_info",
        "add_reaction",
    }


def test_tool_is_registered_in_extension_factories():
    from bytedesk_omnigent.extension import BytedeskExtension

    factories = BytedeskExtension().tool_factories()
    assert "bytedesk_slack" in factories
    tool = factories["bytedesk_slack"](object())
    assert tool.name() == "bytedesk_slack"


_OPS = [
    "post_message",
    "list_channels",
    "channel_history",
    "get_thread",
    "list_users",
    "user_info",
    "add_reaction",
]


@pytest.mark.parametrize("op", _OPS)
def test_every_op_is_dispatchable(op):
    # Each declared op reaches a handler (here: a missing-arg structured error for
    # the ones that require args, or a 200 envelope for the rest), proving the
    # dispatcher routes every enum value rather than falling through to "unknown op".
    tool, _ = _make_tool(
        lambda r: httpx.Response(
            200, json=_ok(channels=[], messages=[], members=[], ts="1.2")
        )
    )
    result = _call(tool, op=op)
    if result["ok"] is False:
        assert "unknown op" not in result["error"]
