"""Implementation of the ``omnigent chat`` command.

The CLI always ends by connecting an Omnigent client to a server URL. For
path targets it first ensures the agent is registered on that server
(a local subprocess by default, or ``--server`` when supplied). URL
targets skip setup and use the existing server's registered agents.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

import click
import httpx
import yaml
from omnigent_client import (
    OmnigentClient,
    SessionToolCallInfo,
    ToolCallable,
    ToolCallInfo,
    ToolHandler,
)
from omnigent_client import (
    OmnigentError as ClientOmnigentError,
)
from omnigent_client._events import (
    ErrorEvent,
    ResponseCancelled,
    ResponseCompleted,
    ResponseFailed,
    ResponseIncomplete,
    TextDelta,
)
from rich.console import Console

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE as _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
)
from omnigent.conversation_browser import open_conversation_link_if_enabled
from omnigent.errors import OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.inner.databricks_executor import _DatabricksBearerAuth, _read_databrickscfg
from omnigent.native_coding_agents import native_coding_agent_for_wrapper_label
from omnigent.spec import load as load_spec
from omnigent.spec._omnigent_compat import OMNIGENT_EXECUTOR_TYPE
from omnigent.spec.parser import discover_host_skills
from omnigent.spec.types import AgentSpec, SkillSpec

if TYPE_CHECKING:
    from omnigent._runner_startup import RunnerStartupProgress

console = Console()

# YAML mapping shape — heterogeneous JSON-shaped values
# (strings, ints, lists, nested dicts) so ``Any`` is the
# narrowest safe element type. Used as the parsed-spec
# return / input shape across this module's helpers.
_YamlMapping: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

logger = logging.getLogger(__name__)

# Local server readiness polling: use a short initial interval so
# freshly-launched ``omnigent run`` sessions don't burn a
# fixed 500 ms before noticing the server is ready, then back off
# slightly while still remaining responsive on slower cold starts.
_SERVER_READY_INITIAL_POLL_SECONDS = 0.05
_SERVER_READY_BACKOFF_POLL_SECONDS = 0.1
_SERVER_READY_FAST_POLL_WINDOW_SECONDS = 1.0

# Remote ``--server`` runners are disposable subprocesses created for
# the CLI session. A one-second grace gives SIGTERM enough time to
# flush runner logs and unregister without noticeably slowing CLI exit.
# Grace period before the CLI escalates SIGTERM → SIGKILL on the
# runner subprocess. Must be long enough for the runner's shutdown
# chain to complete: cancel async tasks → app.router.shutdown() →
# _stop_pm() → _terminal_registry.shutdown() → tmux kill-server
# per session → pm.shutdown() → SIGTERM each harness. 1 s was too
# short — the runner was SIGKILL'd before tmux sessions were reaped,
# leaving zombie codex/claude processes.
_REMOTE_RUNNER_STOP_GRACE_SECONDS = 8.0

# Fallback model when the YAML declares neither ``executor.model``
# nor ``executor.harness`` AND no ``--model`` / ``--harness``
# override is supplied. Mirrors the legacy argparse CLI's
# ``_DEFAULT_AD_HOC_MODEL`` so ``omnigent run examples/hello_world.yaml``
# (a spec with no executor block) launches cleanly instead of
# failing the strict omnigent validator with a cryptic
# "executor.config.harness: required" error.
_DEFAULT_AD_HOC_MODEL = "databricks-gpt-5-4"

# How many of the NEWEST transcript items ``_persisted_turn_text``
# fetches when reconciling a headless ``-p`` turn against the durable
# store. The current turn's items are always the newest, and no single
# one-shot turn emits anywhere near this many items, so the latest turn
# is fully captured regardless of how long a resumed session's history
# is. Fetched ``order="desc"`` (newest first) precisely so the window
# tracks the end of the conversation, not its start.
_RECONCILE_ITEMS_LIMIT = 100

# Optional bearer token for remote omnigent servers that sit
# behind an auth proxy (for example Databricks Apps). When set, the
# CLI sends ``Authorization: Bearer <value>`` on every HTTP request it
# makes to the remote server.
_REMOTE_AUTH_TOKEN_ENV = "OMNIGENT_REMOTE_AUTH_TOKEN"

# Env-var override name. ``OMNIGENT_MODEL=foo`` lets a user
# pin a default model per shell session without needing to pass
# ``--model foo`` on every invocation. Resolved once at spec
# materialization time (not at runtime), so the materialized
# bundle stays self-contained — identical behavior on any host
# that runs the bundle, regardless of that host's env. Mirrors
# the legacy ``_default_cli_model`` at
# ``omnigent/inner/cli.py:344``.
_OMNIGENT_MODEL_ENV_VAR = "OMNIGENT_MODEL"
_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
_OPENAI_BASE_URL_ENV_VAR = "OPENAI_BASE_URL"
_OPENAI_AGENTS_HARNESSES = frozenset({"openai-agents", "openai-agents-sdk"})
_MATERIALIZED_OVERRIDE_DIRS: dict[Path, Path] = {}


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

@dataclass(frozen=True)
class ChatOverrides:
    """
    CLI overrides from ``omnigent run`` flags.

    Applied by materializing a rewritten copy of the agent YAML in a
    temp dir and pointing the local server at that copy — the user's
    source YAML is never mutated.

    :param harness: ``--harness`` value, e.g. ``"claude-sdk"``.
        ``None`` leaves the YAML value unchanged. Written to the flat
        ``executor.harness`` key for single-file omnigent YAMLs and to
        ``executor.config.harness`` for ``spec_version`` bundles (the
        only location that format's parser reads).
    :param model: ``--model`` value, e.g.
        ``"databricks-claude-sonnet-4-6"``. ``None`` unchanged.
    :param system_prompt: ``--system-prompt`` value — overrides the
        YAML's top-level ``prompt`` field (mapped to
        ``AgentSpec.instructions`` by the adapter). ``None``
        unchanged.
    """

    harness: str | None = None
    model: str | None = None
    system_prompt: str | None = None

    @property
    def has_any(self) -> bool:
        """True when at least one override flag was supplied."""
        return any(v is not None for v in (self.harness, self.model, self.system_prompt))

@dataclass(frozen=True)
class LocalServer:
    """
    Handle to a locally-launched omnigent server and its sibling runner.

    Returned by :func:`_start_local_server` so callers can pass the
    handle to :func:`_wait_for_server`, :func:`_stop_local_server`,
    and :func:`_raise_server_failed` without losing track of the
    subprocess's stdout/stderr log path. The log path is the only
    durable record of startup tracebacks (spec parse errors,
    unresolved env vars, executor import failures), so the failure
    helper surfaces it in its error message.

    :param proc: The server subprocess handle.
    :param log_path: Path to the file that captures the
        subprocess's combined stdout/stderr stream,
        e.g. ``Path("~/.omnigent/logs/server/server-abc123.log")``.
    :param runner_id: Stable runner id expected to register over
        the WebSocket tunnel, e.g. ``"runner_0123456789abcdef"``.
    :param runner_proc: The runner subprocess handle, spawned as a
        sibling of the server by :func:`_start_local_server`.
        ``None`` when no runner was started (shouldn't happen in
        normal operation).
    """

    proc: subprocess.Popen[bytes]
    log_path: Path
    runner_id: str | None = None
    runner_proc: subprocess.Popen[bytes] | None = None

@dataclass(frozen=True)
class _SessionToolAdapter:
    """
    Adapt a legacy :class:`ToolHandler` to a sessions-API tool callable.

    :param tool_handler: Legacy client-side tool handler from the
        responses-API path.
    :param agent_name: Agent display name for the legacy
        :class:`ToolCallInfo`, e.g. ``"coding_supervisor"``.
    """

    tool_handler: ToolHandler
    agent_name: str

    def __call__(self, info: SessionToolCallInfo) -> Awaitable[str] | str:
        """
        Execute the legacy tool handler for a sessions-API tool call.

        :param info: Sessions-API tool call context.
        :returns: Tool output string or awaitable string.
        """
        arguments = dict(info.arguments)
        legacy_info = ToolCallInfo(
            name=info.name,
            arguments=arguments,
            call_id=info.call_id,
            agent_name=self.agent_name,
            response_id=info.item_id if info.item_id is not None else info.call_id,
            iteration=0,
        )
        return self.tool_handler.execute(legacy_info)

class _DatabricksTokenAuth(httpx.Auth):
    """
    httpx Auth that authenticates via the Databricks SDK, refreshing
    OAuth tokens transparently.

    Resolution order:
      1. static env-var token (``OMNIGENT_REMOTE_AUTH_TOKEN``)
      2. stored OIDC token (from ``omnigent login``)
      3. Databricks SDK credentials — resolved ONCE and reused, so the
         SDK serves the cached token from memory and only re-runs the
         Databricks CLI near expiry (not on every request).
    """

    def __init__(
        self,
        server_url: str | None = None,
    ) -> None:
        """
        :param server_url: Remote server URL for looking up stored
            OIDC tokens, e.g. ``"http://localhost:6767"``.
        """
        self._server_url = server_url
        raw = os.environ.get(_REMOTE_AUTH_TOKEN_ENV)
        self._static_token = raw.strip() if raw else None
        # Runner-tunnel identity (BDP-2437): a runner launched by the host /
        # connect daemon carries only its tunnel binding token, not a user
        # bearer. The server's RunnerTokenAuthProvider authenticates the runner
        # from the ``X-Omnigent-Runner-Tunnel-Token`` header, so the forwarder
        # (and every runner->server client using this Auth) must send it on
        # EVERY request — independent of, and alongside, any user bearer.
        from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR

        tunnel_raw = os.environ.get(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR)
        self._runner_tunnel_token = tunnel_raw.strip() if tunnel_raw else None
        # Lazily-resolved, then reused, SDK auth (one Config → one token
        # cache). Resolving per request rebuilt Config and shelled out to
        # the Databricks CLI (~0.5s) every time — a heavy tax on the
        # long-lived transcript-forwarder client that posts reply items.
        self._sdk_auth: _DatabricksBearerAuth | None = None
        self._sdk_auth_resolved = False

    def _sdk_token(self) -> str | None:
        """
        Return a bearer token from the reused SDK auth, or ``None``.

        Resolves Databricks SDK auth on first use and reuses it, so
        repeat requests hit the SDK's in-memory token cache instead of
        re-shelling to the Databricks CLI. A stored Databricks Apps
        pointer record for the server (from ``omnigent login
        <apps-url>``) takes precedence over profile/ambient resolution
        — the record names the exact workspace the Apps edge accepts
        tokens from.

        :returns: Bearer token string, or ``None`` when no Databricks
            credentials resolve.
        """
        from omnigent.cli_auth import load_databricks_workspace_host
        from omnigent.inner.databricks_executor import (
            DatabricksAuthError,
            _resolve_databricks_auth,
        )

        if not self._sdk_auth_resolved:
            workspace_host = (
                load_databricks_workspace_host(self._server_url) if self._server_url else None
            )
            try:
                if workspace_host is not None:
                    self._sdk_auth, _host = _resolve_databricks_auth(host=workspace_host)
                else:
                    self._sdk_auth, _host = _resolve_databricks_auth()
            except (DatabricksAuthError, ImportError, ValueError):
                self._sdk_auth = None
            self._sdk_auth_resolved = True
        if self._sdk_auth is None:
            return None
        try:
            return self._sdk_auth.current_token()
        except DatabricksAuthError:
            return None

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """
        Inject an ``Authorization`` header before each request.

        Static env-var token takes precedence, then stored OIDC token,
        then the reused Databricks SDK auth (which refreshes expired
        OAuth tokens transparently).

        :param request: The outgoing httpx request.
        :yields: The request with auth header set.
        """
        # Runner-tunnel identity rides on EVERY request, regardless of which
        # (if any) user bearer is resolved below — the server reads it from a
        # distinct header, so it never collides with ``Authorization``.
        if self._runner_tunnel_token:
            from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER

            request.headers[RUNNER_TUNNEL_TOKEN_HEADER] = self._runner_tunnel_token
        if self._static_token:
            request.headers["Authorization"] = f"Bearer {self._static_token}"
            yield request
            return
        # Check stored OIDC token from `omnigent login`.
        if self._server_url:
            from omnigent.cli_auth import load_token

            oidc_token = load_token(self._server_url)
            if oidc_token:
                request.headers["Authorization"] = f"Bearer {oidc_token}"
                yield request
                return
        token = self._sdk_token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request

@dataclass(frozen=True)
class _AttachSessionInfo:
    """Facts ``attach`` reads from one ``GET /v1/sessions/{id}`` snapshot.

    :param runner_online: ``True`` when the session is bound to a runner the
        server does not report as offline — i.e. a host is live to dispatch
        co-drive turns to. ``attach`` fails loud when ``False``.
    :param agent_name: The session's agent name, e.g. ``"polly"``, used as
        the REPL display name (so ``attach`` never has to pick from the
        server's agent list). ``None`` if the snapshot omits it.
    :param harness: The session's harness, e.g. ``"codex"``, shown in the
        attach banner so it reflects what the host is actually running.
        ``None`` if the snapshot omits it.
    """

    runner_online: bool
    agent_name: str | None
    harness: str | None

@dataclass(frozen=True)
class _DaemonChatSession:
    """A chat session bound to a daemon-spawned runner.

    :param session_id: The created/resolved conversation id, e.g.
        ``"conv_abc123"``.
    :param runner_id: The daemon-spawned runner bound to the session, e.g.
        ``"runner_abc123"``.
    """

    session_id: str
    runner_id: str


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _daemon as _sib_daemon
    from . import _entry as _sib_entry
    from . import _helpers as _sib_helpers
    from . import _local as _sib_local
    from . import _native as _sib_native
    from . import _overrides as _sib_overrides
    from . import _remote as _sib_remote
    from . import _repl as _sib_repl
    from . import _server_proc as _sib_server_proc
    from . import _sessions as _sib_sessions
    for _key, _value in _sib_daemon.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_entry.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_local.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_native.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_overrides.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_remote.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_repl.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_server_proc.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_sessions.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
