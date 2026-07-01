    @app.delete("/v1/sessions/{session_id}/resources/terminals/{terminal_id}")
    async def delete_session_terminal(
        session_id: str,
        terminal_id: str,
    ) -> JSONResponse:
        """Close a terminal resource.

        Idempotent: returns 404 for unknown terminals. Delegates to
        ``TerminalRegistry.close()``.

        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id.
        :returns: Deletion confirmation object.
        """
        closed = await resource_registry.close_terminal(
            session_id,
            terminal_id,
        )
        if not closed:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (f"Terminal {terminal_id!r} not found"),
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "id": terminal_id,
                "object": "session.resource.deleted",
                "deleted": True,
            },
        )

    async def _recreate_repl_terminal(
        session_id: str, terminal_id: str
    ) -> TerminalListEntry | None:
        """Re-create a dead embedded Omnigent REPL terminal for attach.

        The REPL terminal is runner-owned plumbing behind the web UI's
        Terminal view. Its tmux session dies whenever the REPL process
        exits — the user pressing Ctrl+C inside the REPL, or ``omnigent
        attach`` failing at deferred start — but the registry keeps
        reporting the dead instance as running, so the web Terminal pill
        stays enabled while every attach is rejected, leaving a
        permanently empty pane. Closing the stale entry and re-running
        the auto-create restores a live pane whose REPL boots on the
        very attach that triggered the recreation
        (``tmux_start_on_attach``).

        Serialized per session on ``_repl_terminal_ensure_locks``
        against the session-create bootstrap and concurrent attaches;
        liveness is re-checked under the lock so a racer's fresh
        terminal is reused rather than killed.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param terminal_id: The REPL terminal's resource id
            (``"terminal_tui_main"``), passed through for the stale
            close + final resolve.
        :returns: The live ``TerminalListEntry``, or ``None`` when
            recreation failed (the attach then closes 4404 as before).
        """
        if resource_registry is None or resource_registry.terminal_registry is None:
            return None
        registry = resource_registry.terminal_registry
        lock = _repl_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = registry.get(session_id, _REPL_TERMINAL_NAME, _REPL_TERMINAL_SESSION_KEY)
            if existing is None or not existing.running or not await existing.is_alive():
                # Low-level registry close, not ``close_terminal``: the
                # resource-level scan skips entries whose ``running`` flag
                # is already False (the liveness probe above flips it),
                # which would leave the dead instance's activity watcher
                # and scratch dir behind. ``TerminalRegistry.close`` pops
                # the entry unconditionally and tears the instance down.
                await registry.close(session_id, _REPL_TERMINAL_NAME, _REPL_TERMINAL_SESSION_KEY)
                try:
                    await _auto_create_repl_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                    )
                except Exception:
                    # Broad catch, same rationale as the session-create
                    # bootstrap: a failed relaunch (tmux spawn error, label
                    # PATCH failure) must degrade to the pre-existing 4404
                    # close on this attach — never crash the WS route.
                    _logger.exception(
                        "Failed to recreate omnigent REPL terminal for %s",
                        session_id,
                    )
                    return None
        return resolve_terminal_entry_by_resource_id(session_id, terminal_id, registry)

