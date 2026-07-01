    @app.post("/v1/sessions/{conversation_id}/events")
    async def post_session_events(
        conversation_id: str,
        request: Request,
        stream: bool = Query(default=False),
    ) -> Any:
        """
        Inbound surface for the Omnigent server's post-migration session
        event wire path, ``POST /v1/sessions/{conv}/events``.

        Bodies arrive in the harness's discriminated-union shape
        (``MessageEvent`` / ``InterruptEvent`` / ``ToolResultEvent``
        / ``ApprovalEvent``) — see
        :class:`omnigent.runtime.harnesses._scaffold.InboundEventRequest`.
        The runner inspects the discriminator and dispatches:

        * ``message`` (default) with ``stream=false``: starts a
          background turn task and returns 202; events flow
          through ``GET /v1/sessions/{conv}/stream``.
        * ``message`` with ``stream=true``: returns a
          :class:`StreamingResponse` whose body IS the SSE event
          stream. Used by the harness HTTP client which consumes
          the SSE body synchronously for the ``response.created``
          → dispatch → pairing buffer flow.
        * ``interrupt`` / ``tool_result`` / ``approval``: control
          events forwarded to the harness verbatim. ``stream``
          is ignored for these types.

        :param conversation_id: AP-allocated conversation id from
            the URL path, e.g. ``"conv_abc123"``.
        :param request: The FastAPI request; we read its JSON body
            for type-discriminated dispatch.
        :param stream: When ``True`` and ``type == "message"``,
            return a streaming SSE response instead of 202.
            Defaults to ``False``.
        :returns: Either 202 JSON (fire-and-forget), a
            :class:`StreamingResponse` (``stream=true``), or the
            forwarded harness response (control events). 501 when
            no :class:`HarnessProcessManager` is wired up.
        """
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": (
                        "Runner /v1/sessions/{conv}/events needs a HarnessProcessManager; "
                        "build with create_runner_app(process_manager=...) "
                        "after calling await mgr.start()."
                    ),
                },
            )

        body = await request.json()
        body_type = body.get("type") if isinstance(body, dict) else None
        _logger.info(
            "post_session_events: conv=%s type=%s active=%s buffer_len=%d content_types=%s",
            conversation_id,
            body_type,
            conversation_id in _active_turns,
            len(_session_message_buffers.get(conversation_id, [])),
            [b.get("type") for b in body.get("content", []) if isinstance(b, dict)]
            if isinstance(body, dict)
            else "N/A",
        )
        # ``message`` (and absent discriminator) → streaming path with
        # MCP schema injection + action_required intercept.
        # Other discriminators → forward verbatim as control events.
        if body_type == "message" or body_type is None:
            if not isinstance(body, dict):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_request",
                        "detail": "session message body must be a JSON object",
                    },
                )
            message_body = dict(body)
            message_body["conversation_id"] = conversation_id

            # Take an arrival slot, then wait at the FIFO gate so this
            # conversation's messages reach the turn-vs-buffer decision in
            # arrival order regardless of content-resolution latency
            # (RUNNER_MESSAGE_INGEST.md Part A). The sequence is
            # read-incremented synchronously here, before any await, so it
            # reflects arrival order. Content resolution + the decision then
            # run inside the served slot, serialized per conversation.
            _seq = _ingest_next_seq.get(conversation_id, 0)
            _ingest_next_seq[conversation_id] = _seq + 1
            _cond = _ingest_cond.get(conversation_id)
            if _cond is None:
                _cond = asyncio.Condition()
                _ingest_cond[conversation_id] = _cond
            async with _cond:
                while _ingest_now_serving.get(conversation_id, 0) != _seq:
                    await _cond.wait()
            try:
                _raw_content = message_body.get("content")
                if isinstance(_raw_content, list):
                    message_body["content"] = await _resolve_forwarded_message_content(
                        _raw_content,
                        session_id=conversation_id,
                        server_client=server_client,
                    )

                # Turn sequencing gate (invariant I2: single active turn).
                if conversation_id in _active_turns:
                    if _active_turn_already_loaded_persisted_message(
                        conversation_id,
                        message_body,
                    ):
                        _logger.info(
                            "post_session_events: dropping duplicate forwarded "
                            "persisted message conv=%s item=%s",
                            conversation_id,
                            message_body.get("persisted_item_id"),
                        )
                        return JSONResponse(
                            status_code=202,
                            content={
                                "status": "duplicate",
                                "detail": (
                                    "Message already loaded into active turn history."
                                ),
                            },
                        )
                    _native = _is_native_harness(conversation_id)
                    # A turn parked on a human approval must not be steered
                    # past its gate by an incoming message. The non-native
                    # mid-turn injection forward below would do exactly that:
                    # a parent agent's ``sys_session_send`` to a child blocked
                    # on an elicitation would reach the parked turn as a steer
                    # and let it advance — the parent jumping a human gate it
                    # has no business resolving. While an approval is
                    # outstanding we therefore buffer the message WITHOUT
                    # forwarding it; it rides the post-turn continuation drain
                    # after the human delivers a verdict (accept/decline/
                    # timeout), so nothing is lost and only a real ``approval``
                    # event advances the gate. Applies to human-sent messages
                    # too — you can't jump the gate, but your message waits
                    # rather than being dropped.
                    _awaiting_approval = pending_approvals.has_pending(conversation_id)
                    # Stamp a correlation id so the buffered copy and the
                    # forwarded injection share an id. When the harness
                    # consumes the injection it echoes this id back in an
                    # ``injection.consumed`` marker, and the proxy_stream
                    # relay drops the matching buffered copy — so a consumed
                    # message is delivered exactly once and never also
                    # drives a continuation turn (RUNNER_MESSAGE_INGEST.md
                    # Part B). Native harnesses skip the forward entirely
                    # (Part C), so they don't need a correlation id; neither
                    # does a buffer-only park (no forward will be made).
                    if not _native and not _awaiting_approval:
                        message_body["injection_id"] = f"inj_{uuid.uuid4().hex[:16]}"
                    _logger.info(
                        "post_session_events: buffering message for active turn conv=%s "
                        "native=%s awaiting_approval=%s",
                        conversation_id,
                        _native,
                        _awaiting_approval,
                    )
                    _session_message_buffers.setdefault(
                        conversation_id,
                        [],
                    ).append(message_body)
                    # Mid-turn injection: forward the message to the
                    # harness so the SDK sees it at the next breakpoint
                    # in its tool loop (via the scaffold's injection
                    # queue → executor adapter → enqueue_session_message).
                    # Best-effort — a failed forward means the LLM sees
                    # the message on the next turn instead of mid-chain.
                    #
                    # SKIPPED for native harnesses (Part C): their turns are
                    # instant, so the forward's injection races the turn's
                    # teardown (``_watch_injections`` is cancelled when
                    # ``run_turn`` returns) — the message is then either
                    # never typed or typed by a stray new turn. Native
                    # sessions deliver every message through the
                    # one-at-a-time continuation drain below instead.
                    #
                    # SKIPPED while an approval is parked (``_awaiting_approval``):
                    # forwarding would steer the gated turn past a human
                    # approval (see the buffer-only rationale above).
                    if not _native and not _awaiting_approval and process_manager is not None:
                        try:
                            _hc = await process_manager.get_client(conversation_id, "any")
                            _injection_resp = await _hc.post(
                                f"/v1/sessions/{conversation_id}/events",
                                json=message_body,
                                timeout=5.0,
                            )
                            if _injection_resp.status_code >= 400:
                                _logger.warning(
                                    "post_session_events: mid-turn injection forward rejected "
                                    "conv=%s status=%s body=%s",
                                    conversation_id,
                                    _injection_resp.status_code,
                                    _response_body_preview(_injection_resp),
                                )
                            else:
                                _logger.debug(
                                    "post_session_events: mid-turn injection forward accepted "
                                    "conv=%s status=%s",
                                    conversation_id,
                                    _injection_resp.status_code,
                                )
                        except (httpx.HTTPError, RuntimeError, asyncio.TimeoutError):
                            _logger.debug(
                                "mid-turn injection forward failed for %s; "
                                "LLM will see message on next turn",
                                conversation_id,
                                exc_info=True,
                            )
                    return JSONResponse(
                        status_code=202,
                        content={
                            "status": "buffered",
                            "detail": ("Message buffered; active turn will process it."),
                        },
                    )

                # Make the new user message visible to the turn. On the
                # first touch of a conversation after a runner restart the
                # in-memory cache is empty; seeding it with ONLY this
                # message (the old ``setdefault(conv, []).append(...)``)
                # dropped all prior context — the harness then ran the
                # turn with no history. The claude-sdk harness makes this
                # acute: on a cold session (no live SDK client) it replays
                # the in-memory history verbatim as the prompt, so a
                # one-message cache erases the whole conversation.
                new_item = {
                    "type": "message",
                    "role": message_body.get("role", "user"),
                    "content": message_body.get("content", []),
                }
                if conversation_id in _session_histories:
                    # Warm cache: append the new message as before.
                    _session_histories[conversation_id].append(new_item)
                else:
                    # Cold cache (e.g. the first message after a runner
                    # restart): rehydrate the full prior history from the
                    # store so the turn keeps prior context instead of
                    # running with only this message.
                    #
                    # The just-posted message may already be persisted in the
                    # store (invariant I1, omnigent/server/routes/sessions.py:
                    # persist-before-forward), but in its PRE-resolution body
                    # (e.g. ``file_id`` blocks the runner has since resolved to
                    # ``image_url`` / ``file_data``) — so that reloaded copy
                    # must not be forwarded to a harness. The server hands us
                    # the id of the item it persisted for this turn; drop that
                    # exact item from the reload and append the runner-resolved
                    # ``new_item``. Dedup is by identity, not a role/content
                    # guess (content can't be matched once media is resolved).
                    # Native-terminal forwards skip persist-before-forward and
                    # omit ``persisted_item_id``, so nothing is dropped and the
                    # message is simply appended — never lost, never doubled,
                    # never left unresolved.
                    persisted_item_id = message_body.get("persisted_item_id")
                    loaded = await _load_history_as_input(
                        conversation_id,
                        drop_item_id=persisted_item_id,
                    )
                    loaded.append(new_item)
                    _session_histories[conversation_id] = loaded

                _active_turns[conversation_id] = None
                _logger.info(
                    "post_session_events: starting background turn conv=%s",
                    conversation_id,
                )

                _publish_turn_status(conversation_id, "running")

                if stream:
                    # Streaming mode: return the SSE body synchronously
                    # so the executor can consume response.created,
                    # dispatch tool calls, and pair results inline.
                    # Advisor parity with _run_turn_bg: without it, opted-in
                    # streaming turns would never judge, record, or apply a
                    # per-turn brain-model verdict.
                    _stream_advisor_result = await _run_turn_advisor(
                        message_body,
                        conversation_id,
                        await _advisor_spec_for_session(conversation_id),
                    )
                    # Copy-on-write: the per-turn model override + note must
                    # not mutate the caller's body or the cached history.
                    message_body = dict(message_body)
                    _apply_advisor_for_turn(
                        message_body,
                        conversation_id,
                        _stream_advisor_result,
                        message_body.get("model_override"),
                    )
                    response = await _stream_message_to_harness(message_body, conversation_id)
                    if not isinstance(response, StreamingResponse):
                        _on_proxy_stream_end(
                            conversation_id,
                            error={"message": "harness returned error response"},
                        )
                    return response

                # Fire-and-forget mode: start the turn as a background
                # task. Events flow through GET /stream, not the POST
                # response body. Return 202 immediately.
                _turn_task = asyncio.create_task(
                    _run_turn_bg(message_body, conversation_id),
                    name=f"turn-{conversation_id}",
                )
                _active_turns[conversation_id] = _turn_task
                _turn_task.add_done_callback(
                    _background_tasks.discard,
                )
                _background_tasks.add(_turn_task)

                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "accepted",
                        "detail": "Turn started.",
                    },
                )
            finally:
                # Advance the gate so the next-arriving message for this
                # conversation proceeds — even if this one raised, so a
                # failed resolve/decision can't stall later messages.
                async with _cond:
                    _ingest_now_serving[conversation_id] = _seq + 1
                    _cond.notify_all()

        if body_type == "interrupt":
            # Native harnesses get a key sent to their TUI pane — a forwarded
            # InterruptEvent 404s at the scaffold (the instant turn already
            # returned). Each native handler returns; in-process LLM harnesses
            # go through the cancel floor below.
            _harness = _session_harness_name(conversation_id)
            if _harness == "claude-native":
                return await _handle_claude_native_interrupt(conversation_id)
            if _harness == "codex-native":
                return await _handle_codex_native_interrupt(conversation_id)
            if _harness == "pi-native":
                # The pi-native turn lives in the Pi TUI process; the runner's
                # harness task already returned, so the cancel floor has nothing
                # to cancel. Queue an abort to the resident extension instead.
                return await _handle_pi_native_interrupt(conversation_id)
            # In-process harness: mark interrupted, forward an interrupt to the
            # harness, and force-cancel the runner turn task so the turn ends
            # promptly even if the harness can't honor the interrupt in time.
            await _cancel_inprocess_turn(conversation_id)
            return Response(status_code=204)

        if body_type == "external_session_status":
            data = body.get("data") if isinstance(body, dict) else None
            status = data.get("status") if isinstance(data, dict) else None
            forwarded_output = data.get("output") if isinstance(data, dict) else None
            output = forwarded_output if isinstance(forwarded_output, str) else None
            delivery_ack: _SubagentDeliveryAck | None = None
            # Keep this allowlist in sync with Omnigent server's
            # ``_EXTERNAL_SESSION_STATUS_VALUES``. These events are produced by
            # native terminal forwarders, so AP-forwarded output is the only
            # authoritative transcript source.
            if status in ("running", "waiting", "idle", "failed"):
                _fan_out_child_delta_to_parent(
                    conversation_id,
                    {"type": "session.status", "status": status},
                    latest_assistant_text=output,
                    allow_history_preview_fallback=False,
                )
            if status == "idle":
                # Native transcripts are owned by AP. If Omnigent did not forward
                # output for this idle edge, deliver an explicit empty result
                # rather than inventing content from stale runner history.
                delivery_ack = _mark_subagent_terminal_and_wake(
                    conversation_id,
                    status="completed",
                    output=output if output is not None else "",
                )
            elif status == "failed":
                delivery_ack = _mark_subagent_terminal_and_wake(
                    conversation_id,
                    status="failed",
                    output=output or "Error: native sub-agent turn failed",
                )
            if delivery_ack is not None:
                not_confirmed = _subagent_delivery_not_confirmed_response(
                    delivery_ack,
                    is_runner_known_subagent=conversation_id in _session_sub_agent_names,
                )
                if not_confirmed is not None:
                    return not_confirmed
            return Response(status_code=204)

        if body_type == "stop_session":
            # Omnigent server forwards a "stop session" request here. Native harnesses
            # have a live external process: claude-native hard-kills its tmux
            # pane; codex-native asks Codex app-server to interrupt the active
            # turn (same as interrupt).
            # Routing codex-native through the in-process floor would synthesize
            # a [System: interrupted] marker Codex never emits, desyncing the web
            # mirror from Codex's own session. In-process harnesses run their
            # turn in the runner, so stop = cancel the in-flight turn via the
            # same floor as interrupt (this used to 204 no-op, so the sidebar
            # Stop did nothing for them).
            _harness = _session_harness_name(conversation_id)
            if _harness == "claude-native":
                return await _handle_claude_native_stop(conversation_id)
            if _harness == "codex-native":
                return await _handle_codex_native_interrupt(conversation_id)
            if _harness == "pi-native":
                # Pi has no separate session-kill; abort the active turn via the
                # extension (mirrors codex-native reusing its interrupt handler).
                return await _handle_pi_native_interrupt(conversation_id)
            await _cancel_inprocess_turn(conversation_id)
            return Response(status_code=204)

        if body_type == "effort_change":
            # Omnigent server forwards the persisted reasoning_effort here
            # so harnesses that can't re-read it from store at turn
            # boundaries can propagate it live. Today only claude-
            # native has a live-injection path; other harnesses pick
            # up the persisted value on the next turn and need no
            # runtime side effect, so they 204 here.
            if _session_harness_name(conversation_id) == "claude-native":
                effort = body.get("effort") if isinstance(body, dict) else None
                if effort is not None and not isinstance(effort, str):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "invalid_input",
                            "detail": "Body 'effort' must be a string or null",
                        },
                    )
                return await _handle_claude_native_effort_change(
                    conversation_id,
                    effort,
                )
            return Response(status_code=204)

        if body_type == "model_change":
            # Omnigent server forwards the persisted model_override here so
            # harnesses that can't re-read it from store at turn
            # boundaries can propagate it live. Only claude-native has a
            # live-injection path (typing ``/model`` into its tmux pane).
            # codex-native has no usable programmatic model switch in the
            # shipped codex (no settings RPC; ``/model`` is a multi-step
            # TUI selector), so codex model changes are made in the
            # terminal — see the cost-policy deny message. Other harnesses
            # pick up the persisted value on the next turn and 204 here.
            if _session_harness_name(conversation_id) == "claude-native":
                model = body.get("model") if isinstance(body, dict) else None
                if model is not None and not isinstance(model, str):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "invalid_input",
                            "detail": "Body 'model' must be a string or null",
                        },
                    )
                return await _handle_claude_native_model_change(
                    conversation_id,
                    model,
                )
            return Response(status_code=204)

        if body_type == "compact":
            # Omnigent server forwards explicit /compact here. claude-native
            # and codex-native inject the slash command into the tmux
            # pane so the CLI compacts its own context, and return 200
            # to signal the control was handled in the terminal. Other
            # harnesses 204 no-op — their explicit compaction is an
            # AP-side operation the server runs when the runner does
            # not handle the control (see ``_run_compact_locked``).
            if _session_harness_name(conversation_id) == "claude-native":
                return await _handle_claude_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "codex-native":
                return await _handle_codex_native_compact(conversation_id)
            return Response(status_code=204)

        if body_type == "cost_approval_popup":
            # Omnigent server forwards a cost-budget checkpoint here so it can
            # be answered from the native terminal (a tmux display-popup),
            # not only the web ApprovalCard. The popup resolves the SAME
            # elicitation via the resolve endpoint the web card uses, so
            # whichever surface answers first wins. claude-native and
            # codex-native each pop the modal on their pane (different
            # tmux/AP-config sources, shared launcher); other harnesses
            # 204 no-op (the web card is their only surface).
            elicitation_id = body.get("elicitation_id") if isinstance(body, dict) else None
            message = body.get("message") if isinstance(body, dict) else None
            policy_name = body.get("policy_name") if isinstance(body, dict) else None
            # ``elicitation_id`` is the functional resolve key — reject the
            # event if it's missing. ``message`` is display-only (the modal
            # body) and is always set by the Omnigent server forwarder; fall back
            # to a generic label rather than dropping the (still-answerable)
            # popup if a future caller omits it. ``policy_name`` is the
            # display-only modal header and is optional (a generic header is
            # used when absent).
            if not isinstance(elicitation_id, str) or not elicitation_id:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_input",
                        "detail": "Body 'elicitation_id' must be a non-empty string",
                    },
                )
            popup_message = (
                message if isinstance(message, str) and message else "Approval required"
            )
            popup_policy_name = (
                policy_name if isinstance(policy_name, str) and policy_name else None
            )
            harness = _session_harness_name(conversation_id)
            if harness == "claude-native":
                return await _handle_claude_native_cost_popup(
                    conversation_id, elicitation_id, popup_message, popup_policy_name
                )
            if harness == "codex-native":
                return await _handle_codex_native_cost_popup(
                    conversation_id, elicitation_id, popup_message, popup_policy_name
                )
            return Response(status_code=204)

        # Resolve pending policy approval Futures.
        if body_type == "approval":
            _data = body.get("data") or body
            _elic = _data.get("elicitation_id", "")
            _action = _data.get("action", "")
            _approved = _action == "accept"
            pending_approvals.resolve(_elic, _approved)

        # Control event (interrupt / tool_result / approval): get a
        # harness client for this conversation and POST the body
        # verbatim. ``get_client(... "any")`` matches the steering
        # branch in :func:`post_responses` — the runner doesn't need
        # to know the harness name for an already-spawned subprocess;
        # only spawning a fresh one does.
        try:
            harness_client = await process_manager.get_client(conversation_id, "any")
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "no_harness",
                    "detail": _client_safe_error_detail(exc, context="harness lookup"),
                },
            )
        try:
            resp = await harness_client.post(
                f"/v1/sessions/{conversation_id}/events",
                json=body,
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001
            # Best-effort: the harness subprocess may have already
            # exited (race with natural turn completion) or the
            # forward may have failed transport-side. Surface as
            # 502 so the Omnigent route's "best-effort cancel" branch
            # logs and continues with its own asyncio cancel.
            return JSONResponse(
                status_code=502,
                content={
                    "error": "harness_forward_failed",
                    "detail": _client_safe_error_detail(exc, context="harness event forward"),
                    "event_type": body_type,
                },
            )
        return _forward_harness_response(resp)

    async def _resolve_conversation_id(response_id: str) -> str | None:
        """Resolve response_id → conversation_id from the local cache.

        The cache is populated when ``proxy_stream`` sees
        ``response.created``. Elicitations always follow a turn
        that produces ``response.created``, so the cache is
        always warm for legitimate elicitation replies.

        :param response_id: The harness-assigned response id,
            e.g. ``"resp_abc123"``.
        :returns: The conversation id, or ``None`` if the
            response_id is unknown.
        """
        return _resp_to_conv.get(response_id)

