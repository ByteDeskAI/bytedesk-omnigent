    @app.websocket("/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach")
    async def terminal_resource_attach_ws(
        websocket: WebSocket,
        session_id: str,
        terminal_id: str,
        read_only: bool = Query(default=False),
    ) -> None:
        """Attach to a terminal resource by id via WebSocket.

        Resource-addressed counterpart of the legacy
        ``/v1/sessions/{id}/resources/terminals/{id}/attach`` route.
        Resolves the terminal resource id back to the registry entry
        and bridges the tmux PTY.

        The embedded Omnigent REPL terminal (role
        :data:`OMNIGENT_REPL_TERMINAL_ROLE`) gets recreate-on-attach
        semantics: a dead pane is torn down and relaunched instead of
        rejected, so the web Terminal view always opens onto a live
        REPL (see :func:`_recreate_repl_terminal`). Other terminals
        keep the strict 4404 contract — a dead agent-created terminal
        is meaningful state, not plumbing to resurrect.

        :param websocket: Accepted FastAPI WebSocket.
        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id.
        :param read_only: Pass ``-r`` to tmux and drop inbound
            binary frames when ``True``.
        """
        await websocket.accept()
        entry = resolve_terminal_entry_by_resource_id(
            session_id,
            terminal_id,
            terminal_registry,
        )
        if entry is None or not entry.instance.running or not await entry.instance.is_alive():
            if (
                resource_registry is not None
                and resource_registry.terminal_resource_role(session_id, terminal_id)
                == OMNIGENT_REPL_TERMINAL_ROLE
            ):
                entry = await _recreate_repl_terminal(session_id, terminal_id)
            else:
                entry = None
            if entry is None:
                await websocket.close(
                    code=WS_CLOSE_TERMINAL_NOT_FOUND,
                    reason="terminal resource not found or not running",
                )
                return
        # If a cost-budget approval is still pending when this client attaches
        # (the ASK fired while only the web Chat was open), re-pop it on the
        # now-attaching client. Spawned concurrently — it waits for the tmux
        # client below to register, then pops only if still pending — because
        # the PTY bridge blocks for the connection's lifetime.
        _repop_task = asyncio.create_task(
            _repop_pending_cost_popup_on_attach(
                session_id,
                str(entry.instance.socket_path),
                entry.instance.tmux_target,
            )
        )
        _COST_POPUP_REPOP_TASKS.add(_repop_task)
        _repop_task.add_done_callback(_COST_POPUP_REPOP_TASKS.discard)
        await bridge_tmux_pty_to_websocket(
            websocket,
            socket_path=str(entry.instance.socket_path),
            tmux_target=entry.instance.tmux_target,
            read_only=read_only,
            # Stamp client interactions (attach/detach/keystroke/focus/
            # mouse/resize) on the instance so its idle watcher discounts
            # the client-driven repaints they trigger instead of reading
            # them as agent activity. In-process here (runner owns both the
            # attach bridge and the watcher).
            on_client_interaction=entry.instance.note_client_interaction,
        )

    # ── Phase 3: environment filesystem endpoints ─────────────────

    async def _require_os_env(session_id: str) -> Any | None:
        """Raise HTTP 404 if the session's agent spec has no ``os_env``.

        Guards all Phase-3 filesystem endpoints so that sessions whose
        agent spec does not include an ``os_env`` block receive a clean
        404 rather than falling through to a synthetic default
        environment.  The check is a no-op when no agent spec is
        available (dev/standalone mode where
        ``_resolve_session_agent_spec`` returns ``None``).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :raises HTTPException: HTTP 404 when the resolved spec is not
            ``None`` and its ``os_env`` attribute is ``None``.
        :returns: The resolved agent spec, or ``None`` in dev/standalone
            mode.  Callers can use this to avoid a redundant second
            resolution on the same request.
        """
        spec = await _resolve_session_agent_spec(session_id)
        if spec is not None and getattr(spec, "os_env", None) is None:
            raise HTTPException(
                status_code=404,
                detail="Session agent has no os_env configured; filesystem API unavailable.",
            )
        return spec

