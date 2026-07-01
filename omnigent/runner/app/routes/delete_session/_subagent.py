    async def _recover_sub_agent_name(conv_id: str) -> str | None:
        """Resolve a session's sub-agent name, recovering it if lost.

        The in-memory ``_session_sub_agent_names`` map is populated only on
        ``POST /v1/sessions`` and wiped on a runner restart / cleared on
        session delete. A continuation turn that reaches a harness-resolution
        path after a tunnel reconnect therefore finds it empty and resolves
        the PARENT harness for a child session — respawning the harness and
        tearing down the child's native terminal ("Bridge closed").

        This recovers the identity from the authoritative server snapshot
        (``GET /v1/sessions/{id}`` -> ``sub_agent_name``) and backfills the
        in-memory map so subsequent reads are cheap. Best-effort: a failed
        lookup returns ``None`` (a top-level session, or the snapshot is
        unavailable), preserving the prior behavior.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :returns: The sub-agent name, or ``None`` for a top-level session
            (or when it cannot be resolved).
        """
        cached = _session_sub_agent_names.get(conv_id)
        if cached:
            return cached
        try:
            snapshot = await _session_snapshot(conv_id)
        except Exception:  # noqa: BLE001 — best-effort recovery
            return None
        name = snapshot.sub_agent_name if snapshot is not None else None
        if name:
            _session_sub_agent_names[conv_id] = name
        return name


    async def _post_subagent_wake_notice(parent_id: str, notice: str, child_id: str) -> None:
        """
        POST a framework wake notice to a parent session's event stream.

        Mirrors the timer-firing POST in ``tool_dispatch._timer_loop``: the
        synthetic ``user`` message rides the normal ingest path, which starts
        a continuation turn when the parent is idle or buffers (coalescing
        with any other pending messages into a single later turn) when a turn
        is already active. The completion payload itself already sits in the
        parent inbox; this only delivers the wake signal.

        Delivery is delegated to :func:`_deliver_subagent_wake_post`, which
        checks the response status and retries transient failures (e.g. a
        503 ``RUNNER_UNAVAILABLE`` while the parent's runner tunnel
        reconnects). On terminal failure the debounce flag is released so a
        later completion can retry — no parent turn will run to clear it
        otherwise — and a warning is logged.

        :param parent_id: Parent session to wake, e.g. ``"conv_parent123"``.
        :param notice: The ``[System: ...]`` notice text to inject.
        :param child_id: Completing child session id, included only for log
            context, e.g. ``"conv_child456"``.
        :returns: None.
        """
        delivered = await _deliver_subagent_wake_post(server_client, parent_id, notice)
        if not delivered:
            # A failed wake must not crash turn-end; the inbox keeps the result.
            # Release the debounce flag so a later completion can retry the
            # wake — no parent turn will run to clear it otherwise.
            _subagent_wake_pending.discard(parent_id)
            _logger.warning(
                "Sub-agent wake POST failed for parent=%s child=%s after %d attempt(s); "
                "result remains in the parent inbox until the next wake",
                parent_id,
                child_id,
                _WAKE_POST_MAX_ATTEMPTS,
            )


    def _schedule_subagent_wake(entry: _SubagentWorkEntry) -> None:
        """
        Schedule a wake POST after a child completion lands in the parent inbox.

        Called by ``_mark_subagent_terminal_and_wake`` once per delivery (it
        gates on the not-delivered → delivered transition), and a parent is
        never its own child, so a parent's own turn-end never re-wakes it.

        Debounced per parent: while a wake is outstanding (posted, not yet
        consumed by the parent's next turn start), further completions skip
        posting — a fan-out's results all queue in the one inbox, which a
        single wake turn drains via ``sys_read_inbox``. This prevents the
        wake storm (one /events message per completion) that churns turns and
        trips the executor's per-turn tool-context guard.

        :param entry: The just-delivered terminal sub-agent work entry.
        :returns: None.
        """
        # A session is never its own sub-agent; never wake on self.
        if entry.parent_session_id == entry.child_session_id:
            return
        inbox = _session_inboxes.get(entry.parent_session_id)
        if inbox is None:
            return
        # Debounce: one outstanding wake per parent (cleared at turn start).
        if entry.parent_session_id in _subagent_wake_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Off the event loop (defensive); completion drains on the next turn.
            return
        _subagent_wake_pending.add(entry.parent_session_id)
        # qsize counts the item just delivered by put_nowait (>= 1).
        notice = _format_subagent_wake_notice(
            agent=entry.agent,
            title=entry.title,
            status=entry.status,
            pending=inbox.qsize(),
        )
        _wake_task = loop.create_task(
            _post_subagent_wake_notice(entry.parent_session_id, notice, entry.child_session_id)
        )
        _wake_task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_wake_task)


    def _rewake_parent_if_inbox_stranded(parent_session_id: str) -> None:
        """
        Clear a stuck wake flag on parent idle, re-arming if results remain.

        The wake debounce (``_subagent_wake_pending``) is cleared only at turn
        start. A wake consumed as a mid-turn injection never enters
        ``_run_turn_bg``, so the flag stays stuck with no future turn to clear
        it — and the next completion is then debounced and stranded. This runs
        when the parent idles (turn ended, no buffered continuation), so the
        flag is always released here regardless of inbox state; otherwise a
        wake the parent already drained in that same turn would leave the flag
        set and strand the *next* completion. The recovery wake is only posted
        when the inbox still holds undrained results. (The fan-out coalesce
        path is unaffected: it has no turn here, so this is never reached and
        its single outstanding wake still starts the draining turn.)

        :param parent_session_id: Parent whose turn just ended, e.g.
            ``"conv_parent123"``.
        :returns: None.
        """
        if parent_session_id not in _subagent_wake_pending:
            return
        # Always drop the stale flag: the turn just ended with no continuation,
        # so nothing else will clear it. Leaving it set (even on an emptied
        # inbox) would debounce and strand the next completion.
        _subagent_wake_pending.discard(parent_session_id)
        inbox = _session_inboxes.get(parent_session_id)
        if inbox is None or inbox.empty():
            # Flag cleared; nothing stranded to re-wake on.
            return
        entries = list_subagent_work(parent_session_id)
        if not entries:
            return
        # Use the latest completed child so the notice names a real (agent,
        # title); _schedule_subagent_wake recomputes the count from the inbox.
        latest = max(
            entries,
            key=lambda entry: entry.completed_at if entry.completed_at is not None else 0.0,
        )
        _schedule_subagent_wake(latest)


    def _mark_subagent_terminal_and_wake(
        child_session_id: str, *, status: str, output: str | None
    ) -> _SubagentDeliveryAck:
        """
        Mark a child terminal and wake its parent if a payload was delivered.

        Thin wrapper over ``mark_subagent_work_terminal`` for the turn-end
        call sites: it wakes the parent only on a genuine not-delivered →
        delivered transition, so a re-marked (already-terminal) child or an
        untracked session (e.g. the orchestrator's own turn ending) never
        fires a spurious or looping wake.

        :param child_session_id: Child session id, e.g. ``"conv_child456"``.
        :param status: Terminal status: ``"completed"``, ``"failed"``, or
            ``"cancelled"``.
        :param output: Child output or error text. ``None`` means the
            completion had no assistant text to deliver.
        :returns: Delivery acknowledgement for the terminal report.
        """
        ack = mark_subagent_work_terminal(child_session_id, status=status, output=output)
        if ack.entry is not None and ack.delivered_now:
            _schedule_subagent_wake(ack.entry)
        return ack


