    def _session_harness_name(conv_id: str) -> str | None:
        """
        Resolve the canonical harness name for a session, if known.

        Reads ``_session_spec_cache`` (populated at session start by
        ``POST /v1/sessions/{conv}/start`` and the spawn dispatch path)
        and re-derives the harness name via the same precedence used
        at spawn time: ``executor.config.harness`` first, then
        ``executor.type``, then canonicalized via
        :func:`canonicalize_harness`.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The canonical harness name (e.g. ``"claude-native"``)
            or ``None`` if no spec is cached for this session.
        """
        spec = _session_spec_cache.get(conv_id)
        if spec is None:
            return None
        h = spec.executor.config.get("harness") or spec.executor.type
        return canonicalize_harness(h) or h


    def _publish_turn_status(
        conv_id: str,
        status: str,
        error: dict[str, Any] | None = None,
    ) -> None:
        """
        Publish a turn-lifecycle ``session.status`` edge unless a native
        terminal observer already owns that edge.

        Terminal-backed sessions do not all have the same safe edge source.
        For claude-native, the PTY-activity watcher owns ``running`` and
        ``idle`` because a runner turn only types into Claude Code's pane.
        For codex-native, the runner may publish ``running`` when it accepts
        a web turn for dispatch, but the Codex app-server forwarder owns
        ``idle`` because the runner's injection task returns as soon as Codex
        accepts the message, while the user-visible model turn may still be
        active.

        ``failed`` always publishes: a turn-setup error is not observable
        from terminal activity and must surface regardless of harness.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param status: The status edge, ``"running"`` / ``"idle"`` /
            ``"failed"``.
        :param error: Failure detail dict for a ``"failed"`` edge, carried
            through so a SETUP-phase failure surfaces a real message;
            ``None`` for ``running`` / ``idle``.
        :returns: None.
        """
        # An unresolved spec (``_session_harness_name`` → ``None``) means the
        # session hasn't resolved a terminal-backed harness yet, so no native
        # observer is known and the turn lifecycle is still the only status
        # source — fall through and publish. Suppress only once we positively
        # know the harness/edge is terminal-owned.
        harness = _session_harness_name(conv_id)
        if status != "failed" and harness in {"claude-native", "pi-native"}:
            return
        if status == "idle" and harness == "codex-native":
            return
        event: dict[str, Any] = {"type": "session.status", "status": status}
        if error is not None:
            event["error"] = error
        _publish_event(conv_id, event)


    def _on_proxy_stream_end(
        conv_id: str,
        *,
        error: dict[str, Any] | None = None,
    ) -> None:
        """
        Turn-end bookkeeping called from proxy_stream completion points.

        Removes the session from ``_active_turns``, publishes the
        appropriate ``session.status`` event (``idle`` on success
        or cancellation, ``failed`` on error), and schedules a
        post-turn buffer check.

        For a scaffold (in-process) sub-agent, a *successful* turn end is
        reported to the parent as the terminal completion only when no
        continuation is buffered — otherwise the intermediate turn's text
        would be delivered and the real final synthesis dropped (the
        already-terminal entry short-circuits later delivery). Deferring to
        the continuation's own empty-buffer stream end can't strand the
        result: every ``_run_turn_bg`` exit routes back through here, and
        ``_check_and_start_next_turn`` always starts a turn while the buffer
        is non-empty. The error/interrupt/cancel branches stay unconditional
        — those are genuine terminal outcomes, not intermediate narration.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param error: If the turn ended due to an error, a dict
            with at least a ``"message"`` key. ``None`` for
            successful completion.
        """

        _active_turns.pop(conv_id, None)
        # Skip the idle transient when a buffered message will start a
        # continuation turn immediately — `_check_and_start_next_turn`
        # publishes "running" microseconds later, and the in-between idle
        # otherwise hides the Working indicator on the client.
        # `failed` is always published so a real error is never swallowed.
        has_buffered = bool(_session_message_buffers.get(conv_id))
        was_interrupted = conv_id in _interrupted_sessions
        if was_interrupted:
            _interrupted_sessions.discard(conv_id)
            _append_cancellation_items(conv_id)
            if not has_buffered:
                _publish_turn_status(conv_id, "idle")
        elif error is not None:
            # Carry the failure detail so a SETUP-phase failure (no
            # response.failed event) still surfaces a real error message to
            # clients instead of ending silently. ``failed`` is published
            # for every harness (including claude-native) — see
            # _publish_turn_status.
            _publish_turn_status(conv_id, "failed", error=_normalize_turn_error(error))
        else:
            if not has_buffered:
                _publish_turn_status(conv_id, "idle")
        if was_interrupted:
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="cancelled",
                output="[System: sub-agent interrupted]",
            )
        elif error is not None:
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="failed",
                output=f"Error: sub-agent turn failed: {error.get('message', 'unknown')}",
            )
        elif not _is_native_harness(conv_id) and not has_buffered:
            # Defer the success delivery while a continuation is buffered —
            # see the docstring. The continuation turn's own empty-buffer
            # stream end delivers exactly once with the final assistant text.
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="completed",
                output=_extract_last_assistant_text(conv_id),
            )
        # Belt-and-suspenders: POST the terminal status directly to the
        try:
            loop = asyncio.get_running_loop()
            _cont = loop.create_task(
                _check_and_start_next_turn(conv_id),
            )
            _cont.add_done_callback(_background_tasks.discard)
            _background_tasks.add(_cont)
        except RuntimeError:
            pass


    async def _cancel_active_turn(
        conv_id: str, expected_task: asyncio.Task[None] | None = None
    ) -> bool:
        """Force-cancel a session's in-flight turn task — the cancel floor.

        The scaffold's interrupt only takes effect when the executor adapter
        polls between emitted events, so a turn blocked mid-op — or one whose
        executor has no native interrupt — can hang until natural completion.
        Cancelling the runner turn task (the proven primitive from
        :func:`delete_session`) unwinds the runner side regardless of harness.

        On a cancel during the streaming phase, ``_drain_streaming_response``'s
        ``CancelledError`` handler pops ``_active_turns`` and publishes ``idle``
        — but it does NOT append the cancellation items (synthetic outputs for
        dangling tool calls + the interrupted marker). So when the session was
        interrupted, append them here. The ``_interrupted_sessions`` discard is
        the idempotency token: a natural completion that races the cancel runs
        ``_on_proxy_stream_end``, which discards the flag first, so this block
        then no-ops.

        A cancel during the *setup* phase (before ``_drain_streaming_response``
        is entered) raises ``CancelledError`` — a ``BaseException`` — past
        ``_run_turn_bg``'s ``except Exception``, so neither handler runs and
        ``_active_turns`` is left stale (every later message then buffers and
        the session hangs). Detected by the entry still pointing at this task
        after the await; we run the full terminal bookkeeping via
        ``_on_proxy_stream_end`` to recover.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param expected_task: If given, only cancel when this exact task is
            still the live turn. Guards against cancelling a continuation turn
            that replaced the original (the original completed naturally while
            the caller was forwarding the interrupt) — killing that would orphan
            its dangling tool calls.
        :returns: ``True`` if a running turn was cancelled, ``False`` if there
            was no live turn task (or it was replaced by a continuation).
        """
        turn_task = _active_turns.get(conv_id)
        if not isinstance(turn_task, asyncio.Task) or turn_task.done():
            return False
        if expected_task is not None and turn_task is not expected_task:
            return False
        turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn_task
        if _active_turns.get(conv_id) is turn_task:
            # Setup-phase cancel: no handler cleaned up. _on_proxy_stream_end
            # pops _active_turns, publishes idle (or starts a buffered
            # continuation), and runs the interrupted path (flag-discard +
            # cancellation items) itself, so skip the block below.
            _on_proxy_stream_end(conv_id)
            return True
        if conv_id in _interrupted_sessions:
            _interrupted_sessions.discard(conv_id)
            _append_cancellation_items(conv_id)
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="cancelled",
                output="[System: sub-agent interrupted]",
            )
        return True


    async def _cancel_inprocess_turn(conv_id: str) -> None:
        """Stop an in-process (non-native) harness's in-flight turn.

        Shared by the ``interrupt`` and ``stop_session`` dispatch. No-ops when no
        turn is in flight (a stale interrupted flag would taint the next turn).
        Forward the interrupt to the harness FIRST — while its turn is still
        in-flight — so the harness's interrupt handler engages (cancels the turn
        and drops the claude-sdk session); THEN force-cancel the runner turn task
        as the floor. Order matters: cancelling first closes the runner's harness
        stream, which ends the harness turn, so the later interrupt 404s and the
        session is never dropped — the next message then resumes the abandoned
        turn and the agent runs one message behind.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        """
        target = _active_turns.get(conv_id)
        if not isinstance(target, asyncio.Task) or target.done():
            return
        _interrupted_sessions.add(conv_id)
        try:
            harness_client = await process_manager.get_client(conv_id, "any")
            await harness_client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt"},
                # Bounded under the Omnigent server's 5s stop deadline.
                timeout=3.0,
            )
        except Exception:  # noqa: BLE001 — best-effort: harness may have exited
            _logger.warning(
                "Interrupt forward to harness failed for %s",
                conv_id,
                exc_info=True,
            )
        await _cancel_active_turn(conv_id, expected_task=target)


    async def _check_and_start_next_turn(
        session_id: str,
    ) -> None:
        """
        Drain the message buffer and start a continuation turn.

        Called after a turn ends. If messages were buffered while
        the turn was active, pops the first one and starts a new
        background turn. The background turn's completion will
        recursively call this function.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """

        buf = _session_message_buffers.get(session_id)
        if not buf:
            # Parent going idle: clear any wake-debounce flag left stuck by a
            # mid-turn-consumed injection (re-arming if results are stranded),
            # so the next sub-agent completion can wake the parent.
            _rewake_parent_if_inbox_stranded(session_id)
            return

        if _is_native_harness(session_id):
            # Native harnesses type only the latest user message per turn,
            # so collapsing the buffer to its last entry would drop every
            # earlier message from the terminal. Drain ONE message at a
            # time, in order: this turn delivers ``next_body``, and its
            # completion re-enters here for the next buffered message.
            # No batching — each typed exactly once (RUNNER_MESSAGE_INGEST.md
            # Part C).
            next_body = buf.pop(0)
            if not buf:
                _session_message_buffers.pop(session_id, None)
            _session_histories.setdefault(session_id, []).append(
                {
                    "type": "message",
                    "role": next_body.get("role", "user"),
                    "content": next_body.get("content", []),
                }
            )
        else:
            # LLM harnesses: drain ALL buffered messages into history so
            # rapid-fire user input ("hi", "can", "you", "fix", "bugs")
            # becomes a single continuation turn instead of one turn per
            # word. The harness sees every message via history; the turn
            # responds once.
            all_bodies = list(buf)
            buf.clear()
            _session_message_buffers.pop(session_id, None)

            for body in all_bodies:
                _session_histories.setdefault(session_id, []).append(
                    {
                        "type": "message",
                        "role": body.get("role", "user"),
                        "content": body.get("content", []),
                    }
                )
            next_body = all_bodies[-1]

        # Register the continuation turn BEFORE the await so a
        # concurrent POST sees an active turn (invariant I2).
        _active_turns[session_id] = None

        _publish_turn_status(session_id, "running")

        # Use _run_turn_bg so the continuation turn gets full
        # history, tool schemas, instructions — identical to a
        # first turn. Without this, the harness only sees the
        # raw buffered message with no prior context.
        _turn_task = asyncio.create_task(
            _run_turn_bg(next_body, session_id),
            name=f"turn-cont-{session_id}",
        )
        _active_turns[session_id] = _turn_task
        _turn_task.add_done_callback(
            _background_tasks.discard,
        )
        _background_tasks.add(_turn_task)


