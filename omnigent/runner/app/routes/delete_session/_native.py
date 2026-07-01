    def _is_native_harness(conv_id: str) -> bool:
        """
        Whether this session types messages directly into a terminal.

        Native harnesses (``claude-native`` / ``codex-native`` /
        ``pi-native``) have
        *instant* turns — ``run_turn`` returns as soon as the message is
        typed into the pane — and type only the latest user message per
        turn. The runner's mid-turn forward + collapse-batch continuation,
        designed for LLM harnesses whose turns have real duration, drop
        and duplicate messages for them (the forward's injection races the
        instant turn's teardown; the collapse types only the last buffered
        message). Native sessions therefore take the no-forward,
        one-message-at-a-time delivery path. See
        ``designs/RUNNER_MESSAGE_INGEST.md`` Part C.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: ``True`` for native terminal sessions.
        """
        return is_native_harness(_session_harness_name(conv_id))


    def _wake_parent_after_native_interrupt(conv_id: str) -> None:
        """Mark an interrupted native sub-agent cancelled and wake its parent.

        Shared by the claude/codex native interrupt handlers; a no-op when
        *conv_id* is a top-level session (no one's tracked sub-agent).

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        """
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent interrupted]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "Native interrupt: sub-agent delivery not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )


    async def _handle_claude_native_interrupt(conv_id: str) -> Response:
        """
        Stop a claude-native session by injecting Escape into tmux.

        Claude-native sessions have no in-flight harness turn for the
        scaffold's ``InterruptEvent`` path to cancel — the harness's
        ``run_turn`` returns as soon as the user prompt is pasted
        into the tmux pane, and the actual long-running work (Claude
        generating a response) happens inside the ``claude`` binary
        in the pane. The only way to stop it is sending a key to the
        terminal.

        Sending the Escape is the whole job — no synthetic
        ``[System: interrupted]`` transcript marker is persisted. That
        marker exists for in-process LLM harnesses, where the runner's
        ``_session_histories`` *is* the model's next-turn context, so a
        cut-off turn must be repaired (dangling ``function_call`` items
        get synthetic outputs) and annotated. None of that applies to
        Claude-native: Claude owns its own session, the runner only types
        the latest user message into the pane, and Claude records the
        interrupt in its own transcript (mirrored by the forwarder). The
        web UI's interrupt decoration comes from the harness-agnostic
        ``session.interrupted`` event, not this marker. Persisting it here
        only forged a ``role:"user"`` bubble the user never sent into the
        AP-side mirror, diverging it from Claude's real transcript.

        Status is intentionally NOT synthesized here. The terminal's PTY
        activity watcher is the single source of truth: it emits
        ``session.status: idle`` once the pane quiesces after the Escape,
        and keeps the session ``running`` if the interrupt didn't actually
        stop Claude. Emitting ``idle`` here too (as this used to, back when
        the hook-based status couldn't observe idle-on-Escape) would
        bypass — and desync — the watcher's running/idle dedupe, and could
        strand the UI on ``idle`` while Claude kept working.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: 204 on success. 503 if the tmux target is not yet
            advertised (caller treats this as a best-effort failure).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_interrupt,
        )

        # Resolve the bridge id from the session's labels so
        # ``--resume`` sessions (where bridge_id != conversation_id)
        # land in the right tmux pane. Falls back to ``conv_id`` for
        # legacy single-session bridges; see
        # :func:`_claude_native_bridge_id_for_session`.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: UI stop must feel snappy; a missing
            # tmux.json means there's nothing to interrupt anyway.
            await asyncio.to_thread(inject_interrupt, bridge_dir, timeout_s=1.0)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native interrupt"),
                },
            )
        # No ``_append_cancellation_items``: the synthetic marker is for
        # in-process LLM harnesses only (see docstring). The /events dispatch
        # already keeps native out of ``_interrupted_sessions``.
        # NB: no synthesized ``session.status: idle`` here — the PTY watcher
        # emits idle when the pane quiesces after the Escape (and re-asserts
        # running if the interrupt didn't take). See the docstring.
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)


    async def _handle_codex_native_interrupt(conv_id: str) -> Response:
        """
        Stop a codex-native turn via Codex app-server ``turn/interrupt``.

        Codex's own TUI maps its interrupt key to an app-server request
        carrying the active ``threadId`` and ``turnId``. The web/runner path
        should use that protocol directly instead of guessing at terminal
        keybindings: the Codex app-server validates that the requested turn is
        active and replies after the turn aborts.

        No interrupted marker is synthesized here. Codex records the interrupt
        only as a turn-status edge in its own transcript — not as a message — so
        injecting a ``[System: interrupted]`` bubble into the Omnigent mirror would
        diverge the web UI from Codex's actual session (and never survive a
        ``--resume``). Interruption surfaces via the harness-agnostic
        ``session.interrupted`` event; a durable, faithful indicator is a
        follow-up (persist turn status, render from that — no fabricated
        message). claude-native is unaffected: its badge mirrors Claude Code's
        *own* ``[Request interrupted by user]`` record, which is real.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 204 when no active turn is recorded or the interrupt lands;
            503 when Codex rejects the active-turn interrupt.
        """
        from omnigent.codex_native_app_server import client_for_transport
        from omnigent.codex_native_bridge import (
            CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
            bridge_dir_for_bridge_id,
            read_bridge_state,
        )

        labels = await _session_labels_for_runner_spawn(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_id = labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY) or conv_id
        state = read_bridge_state(bridge_dir_for_bridge_id(bridge_id))
        if state is None:
            _logger.warning("Codex-native interrupt skipped for %s: no bridge state.", conv_id)
            return Response(status_code=204)
        if state.session_id != conv_id:
            _logger.warning(
                "Codex-native interrupt skipped for %s: bridge belongs to %s.",
                conv_id,
                state.session_id,
            )
            return Response(status_code=204)
        if state.active_turn_id is None:
            _logger.info("Codex-native interrupt skipped for %s: no active turn.", conv_id)
            return Response(status_code=204)

        codex_client = client_for_transport(
            state.socket_path,
            client_name="omnigent-codex-native-runner",
        )
        try:
            await codex_client.connect()
            await codex_client.request(
                "turn/interrupt",
                {
                    "threadId": state.thread_id,
                    "turnId": state.active_turn_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - surface active-turn interrupt failures to caller.
            _logger.warning(
                "Codex-native turn/interrupt failed for session=%s thread=%s turn=%s",
                conv_id,
                state.thread_id,
                state.active_turn_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native interrupt"),
                },
            )
        finally:
            with contextlib.suppress(Exception):
                await codex_client.close()
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)


    async def _handle_pi_native_interrupt(conv_id: str) -> Response:
        """
        Stop a pi-native turn by asking the resident Pi extension to abort.

        Pi-native turns live inside the terminal's Pi process. The runner's
        harness task only queues the user's message into the extension inbox
        and returns, so the generic in-process cancel floor has nothing useful
        to cancel. Queue an explicit interrupt payload instead; the extension
        consumes it in the TUI process and calls the active
        ``ExtensionContext.abort()``.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 204 when the interrupt payload was queued; 503 if the
            bridge inbox could not be written.
        """
        from omnigent.pi_native_bridge import bridge_dir_for_session_id, enqueue_interrupt

        try:
            await asyncio.to_thread(
                enqueue_interrupt,
                bridge_dir_for_session_id(conv_id),
            )
        except OSError as exc:
            _logger.warning(
                "Pi-native interrupt failed for session=%s",
                conv_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "pi_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="pi-native interrupt"),
                },
            )
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)


    async def _teardown_session_terminals(conv_id: str) -> None:
        """Close a session's terminal resources and announce their removal.

        Removes each terminal from the registry and publishes
        ``session.resource.deleted`` so clients drop it immediately (the
        server relay persists it, matching ``sys_terminal_close``).
        Without the events the web UI keeps showing a dead terminal whose
        attach fails with "terminal resource not found". Two callers:

        - claude-native stop: runner-side analog of the CLI launcher's
          ``_close_claude_terminal``, for the host-spawned (web-UI-created)
          path which has no CLI wrapper to observe the killed pane.
        - agent-switch ``reset-state``: the switch closes the old agent's
          terminals while the session stays open, so clients must be told.

        Best-effort — a close failure (e.g. the pane is already dead) must
        not fail the caller.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: None.
        """
        from omnigent.entities.session_resources import terminal_resource_id
        from omnigent.runner.tool_dispatch import _publish_terminal_deleted_event

        terminal_registry = resource_registry.terminal_registry
        if terminal_registry is None:
            return
        # Snapshot (name, key) before closing — close_terminal mutates the
        # registry, so iterating it lazily while closing would skip entries.
        terminals = [
            (entry.terminal_name, entry.session_key)
            for entry in terminal_registry.list_for_conversation(conv_id)
        ]
        for terminal_name, session_key in terminals:
            terminal_id = terminal_resource_id(terminal_name, session_key)
            try:
                await resource_registry.close_terminal(conv_id, terminal_id)
            except (RuntimeError, OSError):
                _logger.warning(
                    "Failed to close terminal %s for session %s during stop",
                    terminal_id,
                    conv_id,
                    exc_info=True,
                )
            _publish_terminal_deleted_event(
                conversation_id=conv_id,
                terminal_name=terminal_name,
                session_key=session_key,
                publish_event=_publish_event,
            )


    async def _handle_claude_native_stop(conv_id: str) -> Response:
        """
        Terminate a claude-native session by killing its tmux session.

        This is the runner-side handler for the Omnigent web UI's "Stop
        session" affordance. Unlike
        :func:`_handle_claude_native_interrupt` (a single ``Escape``
        that cancels the current response but leaves the session
        alive), this kills the tmux session outright, ending the
        ``claude`` process and the pane.

        We do *not* synthesize transcript items the way the interrupt
        handler does: killing the pane causes the wrapper's reconnect
        loop to observe the terminal resource disappear and tear the
        session down through its normal end-of-session path. We do
        publish a ``session.status: idle`` event so the web UI's
        "Working…" spinner clears immediately rather than lingering
        until the wrapper notices the pane is gone — Claude's ``Stop``
        hook never fires on a hard kill.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: 204 on success. 503 if the tmux target is not yet
            advertised (caller treats this as a best-effort failure —
            a missing target means there is no live session to kill).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            kill_session,
        )

        # Resolve the bridge id from the session's labels so
        # ``--resume`` sessions (where bridge_id != conversation_id)
        # land on the right tmux socket. Falls back to ``conv_id`` for
        # legacy single-session bridges; see
        # :func:`_claude_native_bridge_id_for_session`.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: the UI stop must feel snappy; a missing
            # tmux.json means there's nothing left to kill anyway.
            await asyncio.to_thread(kill_session, bridge_dir, timeout_s=1.0)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_stop_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native stop"),
                },
            )
        # The pane is dead; on the host-spawned path no CLI wrapper will
        # observe that and tear the terminal resource down, so do it here
        # — otherwise the web UI keeps showing a live terminal for the
        # stopped session.
        await _teardown_session_terminals(conv_id)
        _publish_event(
            conv_id,
            {"type": "session.status", "status": "idle"},
        )
        # Reclaim the work entry deterministically. If this killed session is a
        # sub-agent worker, mark it cancelled now (and auto-wake its parent)
        # rather than waiting on the wrapper's reconnect loop to notice the dead
        # pane — that lag left the parent thinking the worker was still running.
        # A no-op for a top-level session (it is no one's tracked sub-agent).
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "Claude-native stop succeeded but sub-agent delivery was "
                "not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )
        return Response(status_code=204)


    async def _handle_claude_native_effort_change(
        conv_id: str,
        effort: str | None,
    ) -> Response:
        """
        Type ``/effort <level>`` into Claude's tmux pane.

        Claude-native sessions can't read the persisted
        ``reasoning_effort`` field at turn boundaries — the
        ``--effort`` flag on the ``claude`` binary is baked in at
        spawn. To propagate a live change without restarting the
        pane, this helper types Claude Code's built-in slash
        command into the terminal.

        Skipped silently when:

        * *effort* is ``None`` — Claude Code has no slash form for
          "use the spawn default", so a clear only takes effect on
          the next spawn.
        * *effort* is in ``EFFORT_VALUES`` but not in
          ``CLAUDE_EFFORTS`` (i.e. ``none`` / ``minimal``) —
          injecting ``/effort none`` would type a literal Claude's
          TUI rejects.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param effort: New persisted effort level, e.g. ``"high"``;
            ``None`` when the user cleared the override.
        :returns: 204 on success or skip (caller treats both the
            same — persisted value is the authoritative fallback).
            503 if the tmux target isn't yet advertised (best-
            effort failure).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )
        from omnigent.reasoning_effort import CLAUDE_EFFORTS

        if effort is None or effort not in CLAUDE_EFFORTS:
            # Persistence already happened on the Omnigent server; the
            # next spawn will pick up the new value via ``--effort``.
            return Response(status_code=204)
        # Resolve the bridge id from the session's labels so
        # ``/fork`` sessions (where bridge_id != conv_id) land in
        # the right tmux pane. Falls back to ``conv_id`` for legacy
        # single-session bridges — same pattern
        # ``_handle_claude_native_interrupt`` uses.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        command = f"/effort {effort}"
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached; persisted effort still applies on next spawn.
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command=command,
                timeout_s=1.0,
                auto_confirm=True,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_effort_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="claude-native effort change"
                    ),
                },
            )
        return Response(status_code=204)


    async def _handle_claude_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        """
        Type ``/model <name>`` into Claude's tmux pane.

        Claude-native sessions can't read the persisted ``model_override``
        field at turn boundaries — the ``--model`` flag on the
        ``claude`` binary is baked in at spawn. To propagate a live
        change without restarting the pane, this helper types Claude
        Code's built-in slash command into the terminal.

        Skipped silently when *model* is ``None`` or empty / whitespace
        only — Claude Code has no slash form for "use the spawn
        default", so a clear only takes effect on the next spawn.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param model: New persisted model identifier, e.g.
            ``"claude-opus-4-7"``; ``None`` when the user cleared the
            override.
        :returns: 204 on success or skip (caller treats both the
            same — persisted value is the authoritative fallback).
            503 if the tmux target isn't yet advertised (best-effort
            failure).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )

        if model is None or not model.strip():
            # Persistence already happened on the Omnigent server; the
            # next spawn will pick up the new value via ``--model``.
            return Response(status_code=204)
        # Resolve the bridge id from the session's labels so
        # ``/fork`` sessions (where bridge_id != conv_id) land in
        # the right tmux pane. Falls back to ``conv_id`` for legacy
        # single-session bridges — same pattern
        # ``_handle_claude_native_interrupt`` uses.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        command = f"/model {model.strip()}"
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached; persisted model still applies on next spawn.
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command=command,
                timeout_s=1.0,
                auto_confirm=True,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native model change"),
                },
            )
        return Response(status_code=204)


    async def _handle_claude_native_compact(conv_id: str) -> Response:
        """
        Type ``/compact`` into Claude's tmux pane.

        Explicit compaction on a claude-native session must run inside
        Claude Code, which owns its own context window in the terminal.
        The Omnigent server's own compaction path (``compact_conversation_now``)
        would only summarise the AP-side transcript mirror — it cannot
        shrink Claude's real context and would desync the two. So the
        web-UI ``/compact`` is injected as Claude Code's built-in slash
        command, the same way ``/effort`` and ``/model`` are.

        Returns 200 (not 204) on successful injection so the Omnigent server
        can tell the control was handled in the terminal and skip its
        own AP-side compaction. Other harnesses 204 no-op in the
        ``post_session_events`` dispatch and the Omnigent server runs its
        in-process compaction instead.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 200 once ``/compact`` has been typed into the pane.
            503 if the tmux target isn't yet advertised (the pane is
            not attached, so there is nothing to compact).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )

        # Resolve the bridge id from the session's labels so ``/fork``
        # sessions (where bridge_id != conv_id) land in the right tmux
        # pane. Falls back to ``conv_id`` for legacy single-session
        # bridges — same pattern the effort/model handlers use.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached, so there is no live Claude to compact.
            # ``auto_confirm`` is left False — ``/compact`` does not pop
            # a confirmation dialog the way ``/effort`` / ``/model`` do.
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command="/compact",
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native compact"),
                },
            )
        return Response(status_code=200)


    async def _handle_codex_native_compact(conv_id: str) -> Response:
        """
        Type ``/compact`` into Codex's tmux pane.

        Mirrors :func:`_handle_claude_native_compact` for codex-native
        sessions.  Codex owns its own context window in the terminal,
        so explicit compaction must be injected as the ``/compact``
        slash command — the same rationale as the claude-native path.

        The tmux pane coordinates come from the **resource registry**
        (not a ``tmux.json`` sidecar) because codex-native terminals
        are launched through the registry.  This is the same resolution
        path :func:`_handle_codex_native_cost_popup` uses.

        Returns 200 on successful injection so the Omnigent server
        knows the control was handled in the terminal and skips its
        own AP-side compaction.  204 when no live terminal is
        registered (the server falls back to in-process compaction).

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 200 once ``/compact`` has been typed into the pane.
            204 if no live codex terminal is registered for the session.
            503 if the tmux send-keys invocation fails.
        """
        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            # No live codex terminal — let the server run AP-side compaction.
            return Response(status_code=204)

        socket_path = str(instance.socket_path)
        target = instance.tmux_target

        try:
            await asyncio.to_thread(_inject_codex_compact, socket_path, target)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native compact"),
                },
            )
        return Response(status_code=200)


    def _inject_codex_compact(socket_path: str, target: str) -> None:
        """
        Blocking helper: type ``/compact`` into a codex tmux pane.

        Uses the same ``C-u`` → literal ``/compact`` → ``Enter``
        sequence that :func:`~omnigent.claude_native_bridge.inject_slash_command`
        uses for claude-native.  Factored into its own function so
        :func:`_handle_codex_native_compact` can run it via
        ``asyncio.to_thread`` without importing at call time.

        :param socket_path: Absolute path to the tmux socket, e.g.
            ``"/tmp/.../codex-main.sock"``.
        :param target: Tmux target pane, e.g. ``"main"``.
        :raises RuntimeError: If any ``tmux send-keys`` invocation fails.
        """
        from omnigent.claude_native_bridge import _run_tmux

        # Clear any draft the user is mid-typing.
        _run_tmux(socket_path, "send-keys", "-t", target, "C-u")
        # Paste ``/compact`` literally.
        _run_tmux(socket_path, "send-keys", "-l", "-t", target, "/compact")
        # Submit.
        _run_tmux(socket_path, "send-keys", "-t", target, "Enter")


    async def _handle_claude_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        """
        Overlay a cost-budget approval modal on Claude's tmux pane.

        A server-side tool-policy ASK (the ``TOOL_CALL`` gate, e.g. a
        cost-budget warning checkpoint) parks and is published to the
        web UI as an ``ApprovalCard``. For a user driving the session in the native
        terminal — who never sees the web card — the Omnigent server forwards a
        ``cost_approval_popup`` control event here, and this handler pops
        a ``tmux display-popup`` modal in the pane. The popup resolves the
        **same** elicitation via the same endpoint the web card uses, so
        whichever surface answers first wins and the other clears. The
        server-side approval Future (and its decline-on-timeout → stop
        behaviour) is unchanged — this only adds a second answer surface.

        Best-effort: the modal is fired detached (it does not block this
        handler), and a pane that isn't attached / a tmux too old for
        ``display-popup`` simply leaves the web card as the only surface.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param elicitation_id: Outstanding elicitation correlation id,
            e.g. ``"elicit_deadbeef"``.
        :param message: Approval reason to display, e.g.
            ``"Session cost $0.12 crossed the $0.10 checkpoint. Continue?"``.
        :param policy_name: Name of the deciding policy, rendered as the
            modal header. ``None`` falls back to a generic header.
        :returns: 204 once the popup has been dispatched (or skipped when
            the pane isn't advertised). 503 only if resolving the bridge
            target raised — a best-effort failure the web card covers.
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            display_cost_approval_popup,
        )

        # Resolve the bridge id from the session's labels so ``/fork``
        # sessions (where bridge_id != conv_id) land in the right tmux
        # pane. Falls back to ``conv_id`` for legacy single-session
        # bridges — same pattern the effort/model/compact handlers use.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached, so there is no client to render the modal — the
            # web ApprovalCard is the only surface and that is fine.
            await asyncio.to_thread(
                display_cost_approval_popup,
                bridge_dir,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_cost_popup_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native cost popup"),
                },
            )
        return Response(status_code=204)


    async def _handle_codex_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        """
        Overlay a cost-budget approval modal on Codex's tmux pane.

        The codex-native counterpart of
        :func:`_handle_claude_native_cost_popup`. Codex does not advertise
        a ``tmux.json`` (its terminal is launched through the resource
        registry), so the pane's socket/target come from the registry
        instance — the same source the web-terminal attach uses — and AP
        routing comes from this bridge's ``policy_hook.json``. Resolution
        differs; the actual popup launch is the shared, harness-agnostic
        :func:`omnigent.native_cost_popup.launch_cost_popup`.

        Best-effort: skips (204) when no live codex terminal is registered
        for the session, so the web ApprovalCard remains the surface.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param elicitation_id: Outstanding elicitation correlation id,
            e.g. ``"elicit_deadbeef"``.
        :param message: Approval reason to display.
        :param policy_name: Name of the deciding policy, rendered as the
            modal header. ``None`` falls back to a generic header.
        :returns: 204 once the popup is dispatched (or skipped when no
            terminal is registered). 503 if launching raised.
        """
        from omnigent.codex_native_bridge import _POLICY_HOOK_FILE, bridge_dir_for_bridge_id
        from omnigent.native_cost_popup import launch_cost_popup

        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            # No live codex terminal to render on; web card is the surface.
            return Response(status_code=204)
        config_file = bridge_dir_for_bridge_id(conv_id) / _POLICY_HOOK_FILE
        try:
            await asyncio.to_thread(
                launch_cost_popup,
                str(instance.socket_path),
                instance.tmux_target,
                config_file,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_cost_popup_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native cost popup"),
                },
            )
        return Response(status_code=204)


    async def _native_cost_popup_config_file(conv_id: str, harness: str) -> Path:
        """
        Resolve the AP-routing config file the cost popup reads, per harness.

        The popup script reads ``ap_server_url`` + ``ap_auth_headers`` from
        this file: ``permission_hook.json`` in the claude-native bridge dir,
        ``policy_hook.json`` in the codex-native bridge dir.

        :param conv_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :param harness: ``"claude-native"`` or ``"codex-native"``.
        :returns: Path to the harness's AP-routing config file.
        """
        if harness == "claude-native":
            from omnigent import claude_native_bridge as _cnb

            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client, session_id=conv_id
            )
            return _cnb.bridge_dir_for_bridge_id(bridge_id) / _cnb._PERMISSION_HOOK_FILE
        from omnigent import codex_native_bridge as _cxb

        return _cxb.bridge_dir_for_bridge_id(conv_id) / _cxb._POLICY_HOOK_FILE


    async def _repop_pending_cost_popup_on_attach(
        conv_id: str,
        socket_path: str,
        tmux_target: str,
    ) -> None:
        """
        Re-pop a still-pending native approval on a newly attached client.

        Covers the case where the ASK fired while no terminal client was
        attached (the user was in the web Chat), then the user opens the
        Terminal: on attach this re-checks the session snapshot and, if a
        native approval is still outstanding — the server-side policy gate
        (``TOOL_CALL`` / ``LLM_REQUEST``, e.g. a cost-budget checkpoint, or
        the ``REQUEST`` gate a native session enforces via the
        ``UserPromptSubmit`` hook) — pops it on the now-attached client.
        Self-correcting — it only pops while the elicitation is still
        pending, so an already-answered approval is not re-shown. Complements
        the ASK-time forward (which covers clients attached *before* the
        ASK). Best-effort: any miss leaves the web card.

        :param conv_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :param socket_path: tmux socket of the attaching pane.
        :param tmux_target: tmux target of the attaching pane, e.g. ``"main"``.
        :returns: None.
        """
        harness = _session_harness_name(conv_id)
        if harness not in ("claude-native", "codex-native"):
            return
        from omnigent.native_cost_popup import launch_cost_popup, wait_for_tmux_client

        # The attach is in flight when this task starts; wait for the client
        # to register so there is something to render the modal on.
        attached = await asyncio.to_thread(
            wait_for_tmux_client, socket_path, tmux_target, timeout_s=5.0
        )
        if not attached:
            return
        try:
            resp = await server_client.get(f"/v1/sessions/{conv_id}", timeout=10.0)
        except httpx.HTTPError:
            return
        if resp.status_code != 200:
            return
        pending = resp.json().get("pending_elicitations") or []
        # The native popup surfaces the server-side policy gate, which parks
        # and resolves via the same endpoint. Re-pop whichever is pending:
        # the tool-policy gate (tool_call / llm_request — including
        # cost-budget checkpoints) and the request-phase gate (request),
        # which native sessions enforce via the UserPromptSubmit hook. A
        # request-phase ASK typically fires while the user is in the web
        # Chat (no client attached), so the on-attach re-pop is its main
        # path onto the terminal.
        approval = next(
            (
                e
                for e in pending
                if isinstance(e, dict)
                and isinstance(e.get("params"), dict)
                and e["params"].get("phase") in ("request", "tool_call", "llm_request")
            ),
            None,
        )
        if approval is None:
            return
        elicitation_id = approval.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            return
        message = approval["params"].get("message") or "Approval required"
        policy_name = approval["params"].get("policy_name")
        config_file = await _native_cost_popup_config_file(conv_id, harness)
        await asyncio.to_thread(
            launch_cost_popup,
            socket_path,
            tmux_target,
            config_file,
            session_id=conv_id,
            elicitation_id=elicitation_id,
            message=message,
            policy_name=policy_name if isinstance(policy_name, str) and policy_name else None,
        )


