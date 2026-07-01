"""CLI entry point for omnigent."""

from __future__ import annotations

import collections.abc
import contextlib
import copy
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, TypeAlias, cast

import click
import yaml
from pydantic import BaseModel, ConfigDict
from rich import box
from rich.console import Console
from rich.table import Table

from omnigent._startup_profile import StartupProfiler
from omnigent.cli_sandbox import lakebox as _lakebox_alias_group
from omnigent.cli_sandbox import sandbox as _sandbox_group
from omnigent.harness_aliases import canonicalize_harness
from omnigent.host.local_server import (
    _DEFAULT_LOCAL_PORT,
    _pid_alive,
    ensure_local_omnigent_server,
    local_server_status,
    local_server_url_if_healthy,
    server_config_signature,
    stop_local_omnigent_server,
    stop_untracked_local_server,
)
from omnigent.onboarding.sandboxes import available_providers as _sandbox_providers
from omnigent.onboarding.ucode_setup import (
    build_ucode_configure_command,
    find_ucode_command,
    model_gateway_workspace_urls,
)

if TYPE_CHECKING:
    import httpx

    from omnigent._runner_startup import RunnerStartupProgress
    from omnigent.onboarding.ambient import DetectedProvider
    from omnigent.onboarding.provider_config import ProviderEntry


# Any: YAML configs have heterogeneous value types (str, int, list, etc.)
def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

class _HostHttpResult:
    """
    Decoded Omnigent management HTTP response.

    :param status_code: HTTP status code, e.g. ``200``. ``0`` means no
        HTTP response was received because the request failed locally.
    :param body: Decoded JSON object or response text, e.g.
        ``{"data": []}`` or ``"not found"``.
    """

    status_code: int
    body: _HostJsonObject | str

class _HostSessionsTableWidths:
    """
    Column widths for one host status sessions table.

    :param session_id: Width for the ``Session ID`` column, e.g. ``41``.
    :param runner_id: Width for the ``Runner ID`` column, e.g. ``44``.
    :param title: Width for the ``Title`` column, e.g. ``28``.
    :param workspace: Optional width for ``Workspace``, e.g. ``48``.
        ``None`` means the terminal is too narrow to show it.
    """

    session_id: int
    runner_id: int
    title: int
    workspace: int | None

class _HostGroup(click.Group):
    """
    ``host`` group that accepts a server URL as a positional argument.

    ``omnigent host <url>`` is shorthand for ``omnigent host
    --server <url>`` when ``<url>`` is URL-like or the empty local-mode
    marker. A leading positional token that matches a registered
    management subcommand (``status``, ``stop``, ``stop-session``)
    still dispatches to that subcommand, and other unknown tokens fall
    through to Click's normal unknown-command error.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """
        Redirect a leading URL-like positional into ``--server``.

        Click stashes the would-be subcommand name in
        ``ctx.protected_args[0]`` after option parsing. When that token
        is a URL-like positional server value, we feed it to the group
        callback instead of trying to dispatch a subcommand. Interspersed
        parsing is enabled only for that case so options may follow the
        URL (``host <url> --server-arg``); for the subcommand or
        unknown-command case it stays off so trailing options reach the
        subcommand path untouched.

        :param ctx: Click context for the ``host`` group.
        :param args: Raw argument tokens for the group.
        :returns: Remaining args after the group consumes its own.
        """
        if self._leading_token_is_server(ctx, args):
            ctx.allow_interspersed_args = True
        super().parse_args(ctx, args)
        # Resilient parsing (shell completion) must keep default behavior
        # so subcommand names still complete.
        if ctx.resilient_parsing or not ctx.protected_args:
            return ctx.args
        candidate = ctx.protected_args[0]
        if candidate in self.commands:
            return ctx.args
        if not self._token_is_positional_server(candidate):
            return ctx.args
        # Leading token is URL-like: treat it as the server URL.
        if ctx.params.get("server") is not None:
            raise click.UsageError(
                "Pass the server URL either positionally or via --server, not both."
            )
        leftover = ctx.protected_args[1:] + ctx.args
        if leftover:
            raise click.UsageError(f"Unexpected extra argument(s): {' '.join(leftover)}")
        ctx.params["server"] = candidate
        ctx.protected_args = []
        ctx.args = []
        return ctx.args

    def _leading_token_is_server(self, ctx: click.Context, args: list[str]) -> bool:
        """
        Decide whether the leading positional should be a server value.

        Runs a throwaway parse of the group's own options to locate the
        first positional token without committing any results to ``ctx``.
        Returns ``True`` when that token exists, is not a registered
        subcommand, and is a valid positional server value.

        :param ctx: Click context for the ``host`` group.
        :param args: Raw argument tokens for the group.
        :returns: ``True`` if the leading positional is a server value.
        """
        if ctx.resilient_parsing or not args:
            return False
        try:
            _, parsed, _ = self.make_parser(ctx).parse_args(list(args))
        except click.UsageError:
            # Malformed options: let the real parse surface the error.
            return False
        return (
            bool(parsed)
            and parsed[0] not in self.commands
            and self._token_is_positional_server(parsed[0])
        )

    def _token_is_positional_server(self, token: str) -> bool:
        """
        Return whether a token may be used as positional ``host`` server.

        The shorthand intentionally accepts only HTTP(S) server URLs and
        the empty string local-mode marker. Plain words such as
        ``"sessions"`` are more likely command typos, so Click should
        report them as unknown subcommands instead of treating them as
        remote server addresses.

        :param token: Leading positional token, e.g.
            ``"https://example.databricksapps.com"`` or ``""``.
        :returns: ``True`` if the token should bind to ``--server``.
        """
        return token == "" or _is_server_url(token)


def _prompt_stop_local_server() -> None:
    """Ask whether to also stop the detached local Omnigent server after exit.

    The local-mode host daemon spawns a detached, persistent local AP
    server (:func:`ensure_local_omnigent_server`) that survives the daemon's exit
    so sessions and the Web UI stay reachable across ``host`` / ``run``.
    Users expect Ctrl-C to stop "everything", so when a healthy local server
    is still running we prompt to stop it too. Declining — or a
    non-interactive / aborted prompt (EOF, a second Ctrl-C) — leaves it
    running. No-op when no healthy local server is found (never spawned, or
    already stopped).

    :returns: None.
    """
    url = local_server_url_if_healthy()
    if url is None:
        return
    try:
        stop = click.confirm(
            f"\nThe local server at {url} is still running so your sessions and "
            "the Web UI stay reachable across `host`/`run`.\nStop it too?",
            default=False,
        )
    except click.Abort:
        # Non-interactive stdin (EOF) or a second Ctrl-C: leave it running
        # rather than hang. ``click.confirm`` maps both to ``Abort``.
        click.echo()
        stop = False
    if stop:
        stop_local_omnigent_server()
        click.echo(f"Stopped the local server ({url}).")
    else:
        click.echo(f"Left the local server running at {url}.")



