"""Native Slack tool over the Slack Web API (BDP-2405, ADR-0143).

The agent-facing arm of the ``slack-command-center`` integration blueprint —
gives team agents **autonomous** Slack access (a native builtin tool bypasses the
MCP approval gate, so an agent can read channels/threads/users and post messages
or reactions without a human-in-the-loop prompt on every call).

**Read + post only.** This tool deliberately exposes inspection plus message and
reaction writes. It does NOT delete, perform admin operations, or otherwise
destroy — those stay behind a richer ceremony.

**Adapter pattern (ADR-0008).** ``_SlackClient`` is the internal facade that
adapts the Slack Web API to a small set of operations; ``BytedeskSlackTool`` is
the agent-facing dispatcher over it. Swapping the external API (or stubbing it in
tests) means replacing the adapter, not the tool.

**Never crash the turn.** Every failure mode returns a structured
``{"ok": false, "error": ...}`` result rather than raising:

- missing/empty token → ``{"ok": false, "error": "slack_not_configured"}``
- an op that needs a channel with neither a 'channel' arg nor a default →
  ``{"ok": false, "error": "slack_channel_not_configured"}``
- a non-200/transport-level failure → ``{"ok": false, "error": "slack_request_failed"}``
- a bad/unknown op or argument → ``{"ok": false, "error": ...}``

**Slack's envelope IS the error contract.** The Slack Web API always returns HTTP
200 with a JSON body ``{"ok": true/false, "error": ...}``. That envelope already
matches our ``{"ok": ...}`` contract, so this tool returns Slack's JSON directly —
a Slack ``ok: false`` (e.g. ``channel_not_found``) is the canonical surfaced
error and is passed through verbatim, NOT translated. Only a transport-level
failure or a non-200 status becomes ``slack_request_failed``.

**Credentials** are read from the omnigent secret backend (BDP-2303) via
``omnigent.onboarding.secrets.load_secret`` — ``SLACK_BOT_TOKEN`` (then
``BYTEDESK_SLACK_TOKEN``) for auth and ``SLACK_DEFAULT_CHANNEL`` for the optional
default channel. Auth is a Bearer token. Secret **values** are never logged or
echoed back to the agent.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from bytedesk_omnigent.tools._http_adapter import HttpToolClient, first_secret
from omnigent.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

_BASE_URL = "https://slack.com/api"
_DEFAULT_HISTORY_LIMIT = 50
_DEFAULT_LIST_LIMIT = 100
_DEFAULT_CHANNEL_TYPES = "public_channel"

#: Token secret names this tool resolves through the secret backend — the Slack
#: name first, then the ByteDesk-namespaced fallback.
_SECRET_TOKEN = ("SLACK_BOT_TOKEN", "BYTEDESK_SLACK_TOKEN")
#: Default channel for ops that omit an explicit ``channel`` (optional).
_SECRET_DEFAULT_CHANNEL = "SLACK_DEFAULT_CHANNEL"


class SlackNotConfiguredError(RuntimeError):
    """Raised internally when no Slack token is set/empty."""


class SlackChannelNotConfiguredError(RuntimeError):
    """Raised internally when an op needs a channel but none is supplied/configured."""


class _SlackClient(HttpToolClient):
    """Internal Adapter over the Slack Web API (ADR-0008).

    Resolves credentials lazily from the secret backend on first use. The httpx
    client is injectable so tests never touch the network.
    """

    def __init__(
        self,
        *,
        base_url: str = _BASE_URL,
        token: str | None = None,
        default_channel: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._default_channel = default_channel
        self._client = client  # injectable for tests; built lazily otherwise
        self._resolved = token is not None  # creds passed in directly (tests)

    def _resolve_credentials(self) -> None:
        if self._resolved:
            return
        self._token = first_secret(_SECRET_TOKEN)
        if self._default_channel is None:
            from omnigent.onboarding.secrets import load_secret

            self._default_channel = (
                load_secret(_SECRET_DEFAULT_CHANNEL) or ""
            ).strip() or None
        self._resolved = True

    def _require_configured(self) -> None:
        self._resolve_credentials()
        if not self._token:
            raise SlackNotConfiguredError(_SECRET_TOKEN[0])

    def _resolve_channel(self, channel: str | None) -> str:
        self._resolve_credentials()
        target = (channel or self._default_channel or "").strip()
        if not target:
            raise SlackChannelNotConfiguredError(_SECRET_DEFAULT_CHANNEL)
        return target

    def _auth_headers(self, *, json_body: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if json_body:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return headers

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._require_configured()
        resp = self._http().request(
            "GET", path, headers=self._auth_headers(json_body=False), params=params
        )
        return self._envelope(resp)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_configured()
        resp = self._http().request(
            "POST", path, headers=self._auth_headers(json_body=True), json=body
        )
        return self._envelope(resp)

    @staticmethod
    def _envelope(resp: httpx.Response) -> dict[str, Any]:
        """Return Slack's JSON envelope; a non-200 is a transport-level failure.

        Slack always answers 200 with ``{"ok": ...}``; anything else is not a
        Slack-level error we can surface verbatim, so raise to the dispatcher's
        request-failed handler.
        """
        resp.raise_for_status()
        return resp.json()

    # ── operations ────────────────────────────────────────────────────────────

    def post_message(
        self, channel: str | None, text: str, thread_ts: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "channel": self._resolve_channel(channel),
            "text": text,
        }
        if thread_ts:
            body["thread_ts"] = thread_ts
        return self._post("/chat.postMessage", body)

    def list_channels(self, types: str, limit: int) -> dict[str, Any]:
        return self._get(
            "/conversations.list", {"types": types, "limit": limit}
        )

    def channel_history(self, channel: str | None, limit: int) -> dict[str, Any]:
        return self._get(
            "/conversations.history",
            {"channel": self._resolve_channel(channel), "limit": limit},
        )

    def get_thread(self, channel: str | None, ts: str) -> dict[str, Any]:
        return self._get(
            "/conversations.replies",
            {"channel": self._resolve_channel(channel), "ts": ts},
        )

    def list_users(self, limit: int) -> dict[str, Any]:
        return self._get("/users.list", {"limit": limit})

    def user_info(self, user: str) -> dict[str, Any]:
        return self._get("/users.info", {"user": user})

    def add_reaction(
        self, channel: str | None, ts: str, name: str
    ) -> dict[str, Any]:
        return self._post(
            "/reactions.add",
            {"channel": self._resolve_channel(channel), "timestamp": ts, "name": name},
        )


class BytedeskSlackTool(Tool):
    """Autonomous Slack access for team agents (post / read channels, threads, users + react)."""

    def __init__(self, client: _SlackClient | None = None) -> None:
        self._slack = client or _SlackClient()

    @classmethod
    def name(cls) -> str:
        return "bytedesk_slack"

    @classmethod
    def description(cls) -> str:
        return (
            "Work with Slack directly: post a message (optionally in a thread), "
            "list channels, read channel history, read a thread, list users, read "
            "a user, and add an emoji reaction. Read + post only — no delete or "
            "admin. Use this to collaborate where teams already work — post status, "
            "ask questions, react — no human approval prompt. Pick the operation "
            "with 'op'. Channel defaults to SLACK_DEFAULT_CHANNEL; override with "
            "'channel'."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "bytedesk_slack",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": [
                                "post_message",
                                "list_channels",
                                "channel_history",
                                "get_thread",
                                "list_users",
                                "user_info",
                                "add_reaction",
                            ],
                            "description": "Which Slack operation to perform.",
                        },
                        "channel": {
                            "type": "string",
                            "description": (
                                "Channel id (e.g. 'C0123'). Defaults to "
                                "SLACK_DEFAULT_CHANNEL when omitted "
                                "(op=post_message/channel_history/get_thread/add_reaction)."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "description": "Message text (op=post_message).",
                        },
                        "thread_ts": {
                            "type": "string",
                            "description": (
                                "Parent message ts to reply in-thread "
                                "(op=post_message, optional)."
                            ),
                        },
                        "ts": {
                            "type": "string",
                            "description": (
                                "Message timestamp identifying a thread or message "
                                "(op=get_thread/add_reaction)."
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "Emoji name without colons, e.g. 'thumbsup' "
                                "(op=add_reaction)."
                            ),
                        },
                        "user": {
                            "type": "string",
                            "description": "User id, e.g. 'U0123' (op=user_info).",
                        },
                        "types": {
                            "type": "string",
                            "description": (
                                "Comma-separated channel types, e.g. "
                                "'public_channel,private_channel' "
                                f"(op=list_channels, default '{_DEFAULT_CHANNEL_TYPES}')."
                            ),
                            "default": _DEFAULT_CHANNEL_TYPES,
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Page size (op=list_channels/channel_history/"
                                "list_users). Defaults to 100 (50 for channel_history)."
                            ),
                        },
                    },
                    "required": ["op"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del ctx  # Slack identity is the configured bot token, not the agent.
        try:
            args: dict[str, Any] = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "invalid_arguments_json"})

        op = args.get("op")
        try:
            result = self._dispatch(op, args)
        except SlackNotConfiguredError:
            return json.dumps({"ok": False, "error": "slack_not_configured"})
        except SlackChannelNotConfiguredError:
            return json.dumps({"ok": False, "error": "slack_channel_not_configured"})
        except httpx.HTTPError as exc:
            # Non-200 status or transport/network blip — log the type, never the
            # credentials. (Slack-level errors are 200 + ok:false and never land
            # here; they pass through as the canonical surfaced error.)
            logger.warning("slack %s request failed: %s", op, type(exc).__name__)
            return json.dumps({"ok": False, "error": "slack_request_failed"})
        return json.dumps(result)

    @staticmethod
    def _limit(args: dict[str, Any], default: int) -> int:
        try:
            return int(args.get("limit", default))
        except (TypeError, ValueError):
            return default

    def _dispatch(self, op: Any, args: dict[str, Any]) -> dict[str, Any]:
        channel = args.get("channel")

        if op == "post_message":
            text = args.get("text")
            if not text:
                return {"ok": False, "error": "missing required 'text'"}
            return self._slack.post_message(channel, text, args.get("thread_ts"))

        if op == "list_channels":
            return self._slack.list_channels(
                str(args.get("types") or _DEFAULT_CHANNEL_TYPES),
                self._limit(args, _DEFAULT_LIST_LIMIT),
            )

        if op == "channel_history":
            return self._slack.channel_history(
                channel, self._limit(args, _DEFAULT_HISTORY_LIMIT)
            )

        if op == "get_thread":
            ts = args.get("ts")
            if not ts:
                return {"ok": False, "error": "missing required 'ts'"}
            return self._slack.get_thread(channel, ts)

        if op == "list_users":
            return self._slack.list_users(self._limit(args, _DEFAULT_LIST_LIMIT))

        if op == "user_info":
            user = args.get("user")
            if not user:
                return {"ok": False, "error": "missing required 'user'"}
            return self._slack.user_info(user)

        if op == "add_reaction":
            ts = args.get("ts")
            name = args.get("name")
            if not ts or not name:
                return {"ok": False, "error": "missing required 'ts' or 'name'"}
            return self._slack.add_reaction(channel, ts, name)

        return {"ok": False, "error": f"unknown op {op!r}"}
