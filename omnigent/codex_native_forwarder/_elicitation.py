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

def _pending_elicitation_matches_resolution(
    pending: _PendingCodexElicitation,
    *,
    request_id: Any,
    thread_id: str | None,
) -> bool:
    """
    Return whether a Codex resolution targets a pending hook wait.

    :param pending: Pending hook wait metadata.
    :param request_id: Codex ``serverRequest/resolved.requestId``,
        e.g. ``12``.
    :param thread_id: Codex ``serverRequest/resolved.threadId``, e.g.
        ``"thread_abc"``, or ``None`` if absent.
    :returns: ``True`` when the notification matches the same request id
        and, when present, the same thread id.
    """
    if pending.request_id != request_id:
        return False
    return pending.thread_id is None or thread_id is None or pending.thread_id == thread_id

def _pending_elicitation_matches_terminal_turn(
    pending: _PendingCodexElicitation,
    *,
    thread_id: str | None,
    turn_id: str | None,
) -> bool:
    """
    Return whether a terminal Codex turn clears a pending hook wait.

    :param pending: Pending hook wait metadata.
    :param thread_id: Codex terminal event thread id, e.g.
        ``"thread_abc"``, or ``None`` if absent.
    :param turn_id: Codex terminal event turn id, e.g.
        ``"turn_abc"``, or ``None`` if absent.
    :returns: ``True`` when the terminal event shares a concrete turn
        or thread scope with the pending request.
    """
    if pending.thread_id is not None and thread_id is not None and pending.thread_id != thread_id:
        return False
    if pending.turn_id is not None and turn_id is not None:
        return pending.turn_id == turn_id
    if pending.thread_id is not None and thread_id is not None:
        return pending.thread_id == thread_id
    return False

def _is_codex_elicitation_request(event: CodexMessage) -> bool:
    """
    Return whether an app-server frame asks this client for input.

    :param event: Codex app-server envelope.
    :returns: ``True`` for supported server-to-client request methods
        that include a JSON-RPC id.
    """
    return (
        _is_codex_request_id(event.get("id"))
        and isinstance(event.get("method"), str)
        and event["method"] in _CODEX_ELICITATION_REQUEST_METHODS
    )

async def _handle_codex_elicitation_request(
    client: httpx.AsyncClient,
    codex_client: CodexAppServerClient,
    *,
    session_id: str,
    event: CodexMessage,
) -> None:
    """
    Forward one Codex input request to Omnigent and reply to app-server.

    The Omnigent hook publishes the web elicitation and blocks until the
    user answers or the wait budget expires. Non-empty 2xx responses
    are Codex JSON-RPC ``result`` payloads and are sent back to the
    app-server with the original request id. Empty 2xx responses mean
    Omnigent timed out or saw the upstream disconnect, so the forwarder
    leaves the request unanswered for the native Codex UI path.

    :param client: HTTP client for Omnigent hook posts.
    :param codex_client: Connected Codex app-server client used to
        send JSON-RPC results.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event: Codex JSON-RPC request envelope.
    :returns: None.
    """
    request_id = event.get("id")
    result = await _codex_elicitation_hook_result(
        client,
        session_id,
        event=event,
    )
    if result is None:
        return
    await codex_client.respond(request_id, result)

async def _codex_elicitation_hook_result(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    event: CodexMessage,
) -> dict[str, Any] | None:
    """
    POST a Codex-shaped elicitation request and parse its result body.

    Empty 2xx responses mean Omnigent timed out or saw the upstream
    disconnect, so the caller should leave the native Codex request
    unanswered or drop a synthetic prompt.

    :param client: HTTP client for Omnigent hook posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event: Codex JSON-RPC request envelope.
    :returns: Parsed JSON-RPC result payload, or ``None``.
    """
    method = event.get("method")
    request_id = event.get("id")
    response = await _post_codex_elicitation_request(
        client,
        session_id,
        event=event,
    )
    if response is None:
        return None
    if response.status_code >= 400:
        _logger.warning(
            "Codex elicitation hook rejected request: method=%s status=%s body=%s",
            method,
            response.status_code,
            response.text[:512],
        )
        return None
    if not response.content:
        _logger.info(
            "Codex elicitation hook returned empty body; leaving app-server request pending: "
            "method=%s request_id=%r",
            method,
            request_id,
        )
        return None
    try:
        result = response.json()
    except ValueError:
        _logger.warning(
            "Codex elicitation hook returned non-JSON body: method=%s body=%s",
            method,
            response.text[:512],
        )
        return None
    if not isinstance(result, dict):
        _logger.warning(
            "Codex elicitation hook returned non-object result: method=%s result=%r",
            method,
            result,
        )
        return None
    return result

async def _elicitation_retry_sleep(seconds: float) -> None:
    """
    Indirection over :func:`asyncio.sleep` for the elicitation re-POST
    backoff, so tests can stub it without clobbering the process-global
    ``asyncio.sleep``.

    :param seconds: Seconds to sleep, e.g. ``1.0``.
    :returns: None.
    """
    await asyncio.sleep(seconds)

async def _post_codex_elicitation_request(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    event: CodexMessage,
) -> httpx.Response | None:
    """
    POST a Codex server-to-client request to the Omnigent hook endpoint,
    re-POSTing across severed long-polls.

    This is deliberately separate from ``_post_session_event``:
    elicitation hook posts are long-poll request/reply calls, not
    idempotent event writes. Proxies sever long-polls and the server can
    restart mid-wait; a single failed POST used to abandon the prompt to
    the native-TUI path — invisible for a headless sub-agent session.
    Codex elicitation ids are deterministic per (session, method, rpc id),
    so a re-POST of the same envelope re-parks the SAME elicitation
    server-side (keeping the approval card alive) and can collect a
    verdict that landed between attempts via the server's pre-resolved
    tombstone. Retries transport errors and 5xx responses within the
    ``_CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS`` budget; 2xx and 4xx
    responses are final.

    :param client: HTTP client for Omnigent hook posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param event: Codex JSON-RPC request envelope.
    :returns: The final hook response, or ``None`` when the retry budget
        ran out — the caller leaves the native request unanswered, as
        before.
    """
    url = f"/v1/sessions/{url_component(session_id)}/hooks/codex-elicitation-request"
    timeout = httpx.Timeout(
        _CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS,
        connect=_CODEX_ELICITATION_CONNECT_TIMEOUT_SECONDS,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS
    backoff_s = _CODEX_ELICITATION_RETRY_INITIAL_BACKOFF_SECONDS
    while True:
        response: httpx.Response | None = None
        try:
            response = await client.post(url, json=event, timeout=timeout)
        except httpx.HTTPError:
            _logger.warning(
                "Codex elicitation hook POST failed; retrying: method=%s",
                event.get("method"),
                exc_info=True,
            )
        if response is not None and response.status_code < 500:
            return response
        if response is not None:
            # 5xx = proxy gateway error on a severed long-poll, or a
            # restarting server — the verdict may still be pending.
            _logger.warning(
                "Codex elicitation hook returned %s; retrying: method=%s",
                response.status_code,
                event.get("method"),
            )
        if loop.time() + backoff_s >= deadline:
            _logger.warning(
                "Codex elicitation hook retry budget exhausted: method=%s",
                event.get("method"),
            )
            return None
        await _elicitation_retry_sleep(backoff_s)
        backoff_s = min(backoff_s * 2, _CODEX_ELICITATION_RETRY_MAX_BACKOFF_SECONDS)

def _note_native_plan_implementation_prompt(
    forwarder_state: _CodexForwarderState,
    event: CodexMessage,
) -> None:
    """
    Dedupe against Codex builds that emit the Plan prompt natively.

    The current Codex TUI owns the final Plan-mode picker locally, but
    if a future app-server starts emitting it as ``requestUserInput``,
    the Omnigent bridge should relay that native request and skip its
    synthetic fallback for the same turn.

    :param forwarder_state: Mutable forwarder state.
    :param event: Codex server-to-client request envelope.
    :returns: None.
    """
    if event.get("method") != _CODEX_TOOL_REQUEST_USER_INPUT_METHOD:
        return
    params = event.get("params")
    if not isinstance(params, dict):
        return
    if not _is_plan_implementation_request_user_input(params):
        return
    turn_id = _turn_id_from_payload(params)
    if turn_id is not None:
        forwarder_state.mark_prompted(turn_id)

def _is_plan_implementation_request_user_input(params: dict[str, Any]) -> bool:
    """
    Return whether ``requestUserInput`` is the Plan implementation picker.

    :param params: Codex ``item/tool/requestUserInput`` params.
    :returns: ``True`` for the final Plan-mode implementation prompt.
    """
    questions = params.get("questions")
    if not isinstance(questions, list):
        return False
    for question in questions:
        if not isinstance(question, dict):
            continue
        if question.get("id") == _PLAN_IMPLEMENTATION_QUESTION_ID:
            return True
        if question.get("question") == _PLAN_IMPLEMENTATION_TITLE:
            return True
    return False

async def _maybe_handle_plan_implementation_prompt(
    client: httpx.AsyncClient,
    codex_client: CodexAppServerClient,
    *,
    session_id: str,
    bridge_dir: Path,
    params: dict[str, Any],
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Publish and resolve the Plan-mode implementation prompt in Omnigent Web.

    Codex's terminal UI asks ``Implement this plan?`` after a completed
    Plan-mode turn, but that picker is local to the TUI. The app-server
    does emit the completed ``plan`` item, so the forwarder synthesizes
    the same user-facing question through the existing Codex
    ``requestUserInput`` hook and starts the selected follow-up turn.

    :param client: HTTP client for Omnigent hook posts.
    :param codex_client: Connected Codex app-server client.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory.
    :param params: Codex ``turn/completed`` params.
    :param forwarder_state: Mutable forwarder state.
    :returns: None.
    """
    turn_id = _turn_id_from_payload(params.get("turn")) or _turn_id_from_payload(params)
    if turn_id is None:
        return
    context = forwarder_state.plan_prompt_context(turn_id)
    if context is None:
        return
    thread_id, plan_text = context
    forwarder_state.mark_prompted(turn_id)
    result = await _codex_elicitation_hook_result(
        client,
        session_id,
        event=_plan_implementation_request_event(thread_id, turn_id),
    )
    selected = _selected_plan_implementation_answer(result)
    if selected == _PLAN_IMPLEMENTATION_NO or selected is None:
        return
    if selected == _PLAN_IMPLEMENTATION_YES:
        await _start_plan_implementation_turn(
            codex_client,
            bridge_dir=bridge_dir,
            thread_id=thread_id,
            text=_PLAN_IMPLEMENTATION_CODING_MESSAGE,
            forwarder_state=forwarder_state,
        )
        return
    if selected == _PLAN_IMPLEMENTATION_CLEAR_CONTEXT:
        await _start_clear_context_plan_implementation_turn(
            codex_client,
            bridge_dir=bridge_dir,
            plan_text=plan_text,
            forwarder_state=forwarder_state,
        )

def _plan_implementation_request_event(thread_id: str, turn_id: str) -> CodexMessage:
    """
    Build a Codex ``requestUserInput`` request for the Plan prompt.

    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :param turn_id: Codex turn id that produced the plan, e.g.
        ``"turn_123"``.
    :returns: Codex JSON-RPC request envelope.
    """
    return {
        "id": f"plan_implementation:{turn_id}",
        "method": _CODEX_TOOL_REQUEST_USER_INPUT_METHOD,
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "itemId": f"{turn_id}:plan_implementation",
            "questions": [
                {
                    "id": _PLAN_IMPLEMENTATION_QUESTION_ID,
                    "header": "Plan",
                    "question": _PLAN_IMPLEMENTATION_TITLE,
                    "isOther": False,
                    "isSecret": False,
                    "options": [
                        {
                            "label": _PLAN_IMPLEMENTATION_YES,
                            "description": "Switch to Default and start coding.",
                        },
                        {
                            "label": _PLAN_IMPLEMENTATION_CLEAR_CONTEXT,
                            "description": "Fresh thread with this plan.",
                        },
                        {
                            "label": _PLAN_IMPLEMENTATION_NO,
                            "description": "Continue planning with the model.",
                        },
                    ],
                }
            ],
        },
    }

def _selected_plan_implementation_answer(result: dict[str, Any] | None) -> str | None:
    """
    Extract the selected Plan prompt label from a Codex hook result.

    :param result: Codex ``requestUserInput`` result payload.
    :returns: Selected option label, or ``None`` when absent.
    """
    if result is None:
        return None
    answers = result.get("answers")
    if not isinstance(answers, dict):
        return None
    question_answer = answers.get(_PLAN_IMPLEMENTATION_QUESTION_ID)
    if not isinstance(question_answer, dict):
        return None
    values = question_answer.get("answers")
    if not isinstance(values, list) or not values:
        return None
    selected = values[0]
    return selected if isinstance(selected, str) and selected else None

async def _start_plan_implementation_turn(
    codex_client: CodexAppServerClient,
    *,
    bridge_dir: Path,
    thread_id: str,
    text: str,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Start a Codex Default-mode implementation turn on an existing thread.

    :param codex_client: Connected Codex app-server client.
    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :param text: User input for the turn.
    :param forwarder_state: Mutable state with the current model.
    :returns: None.
    """
    collaboration_mode = _default_collaboration_mode(forwarder_state)
    if collaboration_mode is None:
        _logger.warning("Codex plan implementation skipped: current model is unknown")
        return
    response = await codex_client.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            "collaborationMode": collaboration_mode,
        },
    )
    turn_id = response.get("result", {}).get("turn", {}).get("id")
    if isinstance(turn_id, str) and turn_id:
        update_active_turn_id(bridge_dir, turn_id)

async def _start_clear_context_plan_implementation_turn(
    codex_client: CodexAppServerClient,
    *,
    bridge_dir: Path,
    plan_text: str,
    forwarder_state: _CodexForwarderState,
) -> None:
    """
    Start a fresh Codex thread and implement the completed plan there.

    :param codex_client: Connected Codex app-server client.
    :param bridge_dir: Native Codex bridge directory.
    :param plan_text: Completed plan markdown from the prior thread.
    :param forwarder_state: Mutable state with the current model.
    :returns: None.
    """
    if not forwarder_state.model:
        _logger.warning(
            "Codex clear-context plan implementation skipped: current model is unknown"
        )
        return
    thread_response = await codex_client.request(
        "thread/start",
        {"model": forwarder_state.model, "sessionStartSource": "clear"},
    )
    thread_id = thread_response.get("result", {}).get("thread", {}).get("id")
    if not isinstance(thread_id, str) or not thread_id:
        _logger.warning("Codex clear-context plan implementation skipped: new thread id missing")
        return
    update_thread_id(bridge_dir, thread_id)
    text = f"{_PLAN_IMPLEMENTATION_CLEAR_CONTEXT_PREFIX}\n\n{plan_text}"
    await _start_plan_implementation_turn(
        codex_client,
        bridge_dir=bridge_dir,
        thread_id=thread_id,
        text=text,
        forwarder_state=forwarder_state,
    )

async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    elicitation_id: str,
) -> bool:
    """
    Post a native-side elicitation resolution signal to AP.

    :param client: HTTP client for Omnigent event posts.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Omnigent elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    :returns: ``True`` when Omnigent accepted the event.
    """
    response = await _post_session_event(
        client,
        session_id,
        event_type=_EXTERNAL_ELICITATION_RESOLVED_TYPE,
        data={"elicitation_id": elicitation_id},
    )
    _log_failed_session_event_post(_EXTERNAL_ELICITATION_RESOLVED_TYPE, response)
    return response is not None and response.status_code < 400


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _collab as _sib_collab
    from . import _deltas as _sib_deltas
    from . import _events as _sib_events
    from . import _fwd_state as _sib_fwd_state
    from . import _helpers as _sib_helpers
    from . import _posting as _sib_posting
    from . import _resume as _sib_resume
    from . import _supervisor as _sib_supervisor
    from . import _turn as _sib_turn
    for _key, _value in _sib_collab.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_deltas.__dict__.items():
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
    for _key, _value in _sib_supervisor.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_turn.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
