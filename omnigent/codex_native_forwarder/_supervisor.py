"""Forward Codex app-server notifications into Omnigent sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from omnigent._native_post_delivery import post_may_have_been_delivered
from omnigent.claude_native_bridge import url_component
from omnigent.codex_native_app_server import (
    CodexAppServerClient,
    CodexMessage,
    client_for_transport,
)
from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    CodexNativeBridgeState,
    clear_active_turn_id_if_matches,
    codex_home_for_bridge_dir,
    read_bridge_state,
    read_codex_config_model,
    update_active_turn_id,
    update_thread_id,
    write_bridge_state,
)
from omnigent.codex_native_elicitation import (
    codex_elicitation_id,
)
from omnigent.codex_native_elicitation import (
    is_codex_request_id as _is_codex_request_id,
)
from omnigent.entities.session_resources import terminal_resource_id

_logger = logging.getLogger(__name__)
def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

async def supervise_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    app_server_url: str,
    thread_id: str,
    client: CodexAppServerClient | None = None,
    auth: httpx.Auth | None = None,
    ap_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """
    Mirror Codex app-server notifications into an Omnigent session.

    :param base_url: Omnigent server base URL, e.g.
        ``"http://127.0.0.1:6767"``.
    :param headers: Static HTTP headers for Omnigent requests.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param app_server_url: Codex app-server transport, e.g.
        ``"ws://127.0.0.1:9876"``. Used to (re)connect a fallback
        client when ``client`` is ``None`` and persisted to bridge
        state on thread rotation, so the executor keeps reaching the
        live app-server after a native ``/clear``.
    :param thread_id: Codex thread id to subscribe to.
    :param client: Optional already-connected client. Fresh Codex
        sessions pass the listener that observed ``thread/started``;
        the forwarder still calls ``thread/resume`` once the id is
        known so that connection receives turn/item notifications.
    :param auth: Optional HTTP auth for long-lived remote sessions.
    :param ap_transport: Optional HTTP transport for the Omnigent client,
        e.g. ``httpx.MockTransport(...)`` for tests.
    :returns: None. Runs until cancelled or the app-server connection
        closes.
    """
    if client is None:
        client = client_for_transport(app_server_url, client_name="omnigent-codex-forwarder")
        await client.connect()
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        auth=auth,
        timeout=httpx.Timeout(30.0),
        transport=ap_transport,
    ) as ap_client:
        target = _ForwarderTarget(
            session_id=session_id,
            thread_id=thread_id,
            delta_coalescer=_OutputTextDeltaCoalescer(ap_client, session_id),
            usage_coalescer=_SessionUsageCoalescer(ap_client, session_id),
            elicitation_tracker=_CodexElicitationTaskTracker(),
        )
        forwarder_state = _CodexForwarderState(
            parent_session_id=session_id,
            codex_client=client,
        )
        # Released when the live event stream shows the thread became
        # active (its first turn materializes the rollout). Lets the
        # subscribe task park instead of blind-polling ``thread/resume``
        # for a fresh, still-empty thread. Recreated per thread on rotation.
        thread_active = asyncio.Event()
        subscribe_task = asyncio.create_task(
            _subscribe_until_ready(
                client,
                ap_client,
                session_id=target.session_id,
                bridge_dir=bridge_dir,
                thread_id=target.thread_id,
                usage_coalescer=target.usage_coalescer,
                elicitation_tracker=target.elicitation_tracker,
                forwarder_state=forwarder_state,
                ready_signal=thread_active,
            ),
            name="codex-native-forwarder-subscribe",
        )
        await _sleep(0)
        try:
            async for event in client.iter_events():
                try:
                    rotated = await _maybe_rotate_session_on_thread_started(
                        ap_client=ap_client,
                        target=target,
                        bridge_dir=bridge_dir,
                        app_server_url=app_server_url,
                        event=event,
                    )
                    if rotated:
                        forwarder_state.note_parent_rotation(target.session_id)
                        subscribe_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await subscribe_task
                        # Fresh thread after a /clear rotation — start its
                        # own active signal so the new subscription parks
                        # until the rotated thread's first turn.
                        thread_active = asyncio.Event()
                        subscribe_task = asyncio.create_task(
                            _subscribe_until_ready(
                                client,
                                ap_client,
                                session_id=target.session_id,
                                bridge_dir=bridge_dir,
                                thread_id=target.thread_id,
                                usage_coalescer=target.usage_coalescer,
                                elicitation_tracker=target.elicitation_tracker,
                                forwarder_state=forwarder_state,
                                ready_signal=thread_active,
                            ),
                            name="codex-native-forwarder-subscribe",
                        )
                        continue
                    # Release the subscribe task as soon as the thread shows
                    # activity (rollout now exists), so it resumes instead of
                    # waiting forever on an idle fresh thread.
                    if not thread_active.is_set() and _event_indicates_thread_active(event):
                        thread_active.set()
                    await _handle_event(
                        ap_client,
                        session_id=target.session_id,
                        bridge_dir=bridge_dir,
                        event=event,
                        delta_coalescer=target.delta_coalescer,
                        usage_coalescer=target.usage_coalescer,
                        elicitation_tracker=target.elicitation_tracker,
                        expected_thread_id=target.thread_id,
                        codex_client=client,
                        forwarder_state=forwarder_state,
                    )
                except Exception:  # noqa: BLE001 - keep the long-lived mirror alive.
                    _logger.warning("Codex forwarder event handling failed", exc_info=True)
        finally:
            await target.delta_coalescer.close()
            await target.usage_coalescer.close()
            await target.elicitation_tracker.close()
            subscribe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await subscribe_task
            await client.close()

async def _maybe_rotate_session_on_thread_started(
    *,
    ap_client: httpx.AsyncClient,
    target: _ForwarderTarget,
    bridge_dir: Path,
    app_server_url: str,
    event: CodexMessage,
) -> bool:
    """
    Rotate Omnigent ownership when Codex starts a new native thread.

    Native Codex ``/clear`` starts a fresh app-server thread in the
    existing terminal. The forwarder must move the Omnigent session binding
    to a fresh conversation and then subscribe this same app-server
    connection to the new thread; otherwise web messages keep targeting
    the old thread and streaming appears to end.

    :param ap_client: Omnigent HTTP client used for session rotation.
    :param target: Mutable current AP/Codex target.
    :param bridge_dir: Native Codex bridge directory.
    :param app_server_url: Codex app-server transport, e.g.
        ``"ws://127.0.0.1:9876"``. Persisted to bridge state for the
        replacement session.
    :param event: Codex app-server notification envelope.
    :returns: ``True`` when rotation occurred.
    """
    new_thread_id = _thread_id_from_started_event(event)
    if new_thread_id is None or new_thread_id == target.thread_id:
        return False
    # A Codex AgentControl child thread emits ``thread/started`` when it
    # begins. That event must not rotate the parent Omnigent session — the child
    # is discovered later via a ``collabAgentToolCall`` item and routed to
    # its own Omnigent child session by ``_handle_event``.
    if _thread_started_is_subagent(event):
        return False
    old_delta_coalescer = target.delta_coalescer
    await old_delta_coalescer.flush()
    old_usage_coalescer = target.usage_coalescer
    await old_usage_coalescer.flush()
    old_elicitation_tracker = target.elicitation_tracker
    old_session_id = target.session_id
    new_session_id = await _create_thread_replacement_session(
        client=ap_client,
        old_session_id=old_session_id,
        bridge_dir=bridge_dir,
        app_server_url=app_server_url,
        new_thread_id=new_thread_id,
    )
    target.session_id = new_session_id
    target.thread_id = new_thread_id
    target.delta_coalescer = _OutputTextDeltaCoalescer(ap_client, new_session_id)
    target.usage_coalescer = _SessionUsageCoalescer(ap_client, new_session_id)
    target.elicitation_tracker = _CodexElicitationTaskTracker()
    await old_delta_coalescer.close()
    await old_usage_coalescer.close()
    await old_elicitation_tracker.close()
    _logger.info(
        "Codex forwarder rotated Omnigent session after native thread switch: "
        "old_session=%s new_session=%s new_thread=%s",
        old_session_id,
        new_session_id,
        new_thread_id,
    )
    return True

async def _create_thread_replacement_session(
    *,
    client: httpx.AsyncClient,
    old_session_id: str,
    bridge_dir: Path,
    app_server_url: str,
    new_thread_id: str,
) -> str:
    """
    Create and activate the Omnigent session for a new native Codex thread.

    :param client: Omnigent HTTP client.
    :param old_session_id: Session being rotated away from, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Codex bridge directory.
    :param app_server_url: Codex app-server transport, e.g.
        ``"ws://127.0.0.1:9876"``. Written to the replacement session's
        bridge state so the executor reaches the live app-server after
        rotation (a unix path here would clobber the ws:// URL).
    :param new_thread_id: Newly started Codex thread id, e.g.
        ``"thread_new"``.
    :returns: New Omnigent session id, e.g. ``"conv_new"``.
    :raises httpx.HTTPStatusError: If Omnigent rejects the create, bind,
        external-session update, or terminal transfer calls.
    :raises RuntimeError: If the old session snapshot or create
        response is malformed.
    """
    old = await _fetch_session_snapshot(client, old_session_id)
    agent_id = old.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise RuntimeError(f"session {old_session_id!r} has no agent_id")
    runner_id = old.get("runner_id")
    labels = old.get("labels") if isinstance(old.get("labels"), dict) else {}
    labels = {str(key): str(value) for key, value in labels.items()}
    state = read_bridge_state(bridge_dir)
    if CODEX_NATIVE_BRIDGE_ID_LABEL_KEY not in labels:
        labels[CODEX_NATIVE_BRIDGE_ID_LABEL_KEY] = old_session_id

    create_resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "labels": labels,
        },
    )
    create_resp.raise_for_status()
    created = create_resp.json()
    new_session_id = created.get("id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise RuntimeError("Codex thread replacement response did not include id")

    if isinstance(runner_id, str) and runner_id:
        bind_resp = await client.patch(
            f"/v1/sessions/{url_component(new_session_id)}",
            json={"runner_id": runner_id},
        )
        bind_resp.raise_for_status()

    external_resp = await client.patch(
        f"/v1/sessions/{url_component(new_session_id)}",
        json={"external_session_id": new_thread_id},
    )
    external_resp.raise_for_status()

    terminal_id = terminal_resource_id("codex", "main")
    transfer_resp = await client.post(
        (
            f"/v1/sessions/{url_component(old_session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}/transfer"
        ),
        json={"target_session_id": new_session_id},
    )
    transfer_resp.raise_for_status()

    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id=new_session_id,
            socket_path=app_server_url,
            thread_id=new_thread_id,
            codex_home=(
                state.codex_home
                if state is not None
                else str(codex_home_for_bridge_dir(bridge_dir))
            ),
        ),
    )

    clear_resp = await client.patch(
        f"/v1/sessions/{url_component(old_session_id)}",
        json={"runner_id": ""},
    )
    if clear_resp.status_code >= 400:
        _logger.warning(
            "Failed to clear old codex-native runner binding after thread switch; "
            "old_session=%s new_session=%s status=%s body=%s",
            old_session_id,
            new_session_id,
            clear_resp.status_code,
            clear_resp.text,
        )
    return new_session_id

async def _fetch_session_snapshot(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
    """
    Fetch an Omnigent session snapshot for Codex session rotation.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :returns: Decoded JSON session snapshot.
    :raises httpx.HTTPStatusError: If Omnigent rejects the request.
    :raises RuntimeError: If the response is not a JSON object.
    """
    resp = await client.get(f"/v1/sessions/{url_component(session_id)}")
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Codex session snapshot response was not an object")
    return payload

async def _subscribe_until_ready(
    client: CodexAppServerClient,
    ap_client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    thread_id: str,
    usage_coalescer: _SessionUsageCoalescer,
    elicitation_tracker: _CodexElicitationTaskTracker,
    forwarder_state: _CodexForwarderState | None = None,
    ready_signal: asyncio.Event | None = None,
) -> None:
    """
    Subscribe this app-server connection to a Codex thread.

    A resume session's thread already has a persisted rollout, so the
    first ``thread/resume`` succeeds and any prior message items are
    replayed immediately.

    A fresh TUI-created thread, however, has *no* rollout until its first
    turn runs — Codex defers materialization for a new thread, so
    ``thread/resume`` rejects it with ``no rollout found``. Rather than
    blind-poll that state (which hammers the app-server for the entire
    idle window before the user's first turn), this parks on
    *ready_signal* and only retries once the caller observes the thread
    become active on the live event stream (its first turn, which
    materializes the rollout). ``thread/status/changed``/turn/item events
    reach the connection without a successful resume, so the caller can
    detect activity and set the signal. A short poll still covers the
    brief window between "thread active" and the rollout being flushed.

    :param client: Codex app-server client.
    :param ap_client: Omnigent HTTP client used for replayed items.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: Codex thread id.
    :param usage_coalescer: Token-usage coalescer for replayed
        app-server events.
    :param elicitation_tracker: Background Codex elicitation tracker.
    :param forwarder_state: Optional mutable forwarder state that
        receives thread metadata from the resume response.
    :param ready_signal: Set by the caller when it observes the thread
        become active (rollout now exists). While unset, a not-ready
        thread parks here instead of polling. ``None`` falls back to the
        fixed-interval retry (used where no live event stream drives the
        signal).
    :returns: None.
    """
    saw_not_ready = False
    while True:
        try:
            params: dict[str, Any] = {"threadId": thread_id}
            if not saw_not_ready:
                params["excludeTurns"] = True
            response = await client.request("thread/resume", params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - app-server error envelopes are surfaced as RuntimeError.
            if _is_thread_not_ready_error(exc):
                if not saw_not_ready:
                    _logger.info(
                        "Codex thread %s is not ready yet (no/empty rollout); "
                        "retrying subscription",
                        thread_id,
                    )
                saw_not_ready = True
                if ready_signal is not None and not ready_signal.is_set():
                    # Idle fresh thread: no rollout until the first turn, and
                    # no reason to poll meanwhile. Park until the caller's
                    # event loop observes the thread go active.
                    await ready_signal.wait()
                else:
                    # No signal wired (fallback), or the thread is active but
                    # its rollout isn't flushed yet (brief race) — a short
                    # poll covers that window.
                    await _sleep(_SUBSCRIBE_RETRY_DELAY_SECONDS)
                continue
            _logger.warning("failed to subscribe to Codex thread %s", thread_id, exc_info=True)
            return
        if forwarder_state is not None:
            forwarder_state.note_resume_response(response)
            # Source of truth for the cost policy is config.toml's model (what
            # /model writes). Read it now so model_override reflects it from
            # the first tool call, not a turn later. Falls back to the resume
            # response's model when config.toml has none.
            _refresh_model_from_config(bridge_dir, forwarder_state)
            await _sync_model_change(
                ap_client, session_id=session_id, forwarder_state=forwarder_state
            )
        await _replay_resume_response(
            ap_client,
            session_id=session_id,
            bridge_dir=bridge_dir,
            response=response,
            usage_coalescer=usage_coalescer,
            elicitation_tracker=elicitation_tracker,
            forwarder_state=forwarder_state,
        )
        return

def _event_indicates_thread_active(event: CodexMessage) -> bool:
    """
    Return whether an app-server notification implies the thread is now active.

    A fresh thread's rollout is only materialized once its first turn
    starts, so the subscription's ``thread/resume`` keeps failing until
    then. These notifications all imply a turn has begun (hence the
    rollout now exists), and — crucially — they reach a connection
    *without* a successful resume, so the forwarder's main loop can use
    them to release :func:`_subscribe_until_ready` from its parked wait:

    - any ``turn/*`` or ``item/*`` notification, and
    - ``thread/status/changed`` transitioning to an ``active`` status.

    :param event: A Codex JSON-RPC notification envelope.
    :returns: ``True`` if the event implies the thread became active.
    """
    method = event.get("method")
    if not isinstance(method, str):
        return False
    if method.startswith(("turn/", "item/")):
        return True
    if method == "thread/status/changed":
        params = event.get("params")
        status = params.get("status") if isinstance(params, dict) else None
        return isinstance(status, dict) and status.get("type") == "active"
    return False

def _is_thread_not_ready_error(exc: Exception) -> bool:
    """
    Return whether a subscription failure is Codex's fresh-thread not-ready gap.

    Covers the two transient states a freshly created thread passes through
    before its first turn populates the rollout: the rollout file is missing
    (``no rollout found for thread id``) or present-but-empty
    (``... rollout ... is empty``). Both are retryable — once a turn writes
    the rollout, ``thread/resume`` succeeds.

    :param exc: Exception raised by ``thread/resume``.
    :returns: ``True`` for either retryable not-ready state.
    """
    message = str(exc)
    if _NO_ROLLOUT_FRAGMENT in message:
        return True
    return "rollout" in message and _EMPTY_ROLLOUT_FRAGMENT in message


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _collab as _sib_collab
    from . import _deltas as _sib_deltas
    from . import _elicitation as _sib_elicitation
    from . import _events as _sib_events
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _posting as _sib_posting
    from . import _resume as _sib_resume
    from . import _turn as _sib_turn
    for _key, _value in _sib_collab.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_deltas.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_elicitation.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_events.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_fwd_state.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_posting.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_resume.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_turn.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
