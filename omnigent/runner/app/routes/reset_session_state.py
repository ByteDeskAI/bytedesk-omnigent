    @app.post("/v1/sessions/{session_id}/reset-state")
    async def reset_session_state(session_id: str) -> JSONResponse:
        """Reset runner-side session state after an in-place agent switch.

        Runner-internal endpoint the AP server calls (once, while the
        session is idle) right after rebinding a conversation to a new
        agent.  It switches the session onto the new agent's os_env while
        preserving the workspace files:

        1. Closes the session's terminals via
           :func:`_teardown_session_terminals`, publishing
           ``session.resource.deleted`` for each so connected clients
           drop them (without the events the web UI keeps showing the
           old agent's dead terminal), then closes the primary OSEnv via
           :meth:`SessionResourceRegistry.cleanup_session` (workspace
           files are preserved).  The primary env re-materializes lazily
           on the next access from the new agent's spec, so the new
           ``os_env`` / sandbox / fork policy take effect while ``cwd``
           stays pinned to the same runner workspace.
        2. Drops the spec-derived session caches so the next access
           re-resolves the new agent.  The web filesystem/shell endpoints
           build the primary env from ``_session_spec_cache`` (keyed via
           ``_session_snapshot_cache``'s ``agent_id``); without dropping
           these the env would just rebuild from the STALE old spec and
           the new sandbox would never apply (a cross-agent sandbox
           leak).  Mirrors the turn-path switch reset.

        ``_session_agent_ids`` is deliberately left intact so the next
        turn still detects the switch and cold-starts the new harness
        subprocess.  This is a separate endpoint from
        ``DELETE /resources`` so the session-deletion contract (which
        also closes resources but never needs the switch-specific cache
        reset) is untouched.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: Confirmation that the switch reset was applied.
        """
        _codex_terminal_ensure_locks.pop(session_id, None)
        _claude_terminal_ensure_locks.pop(session_id, None)
        _pi_terminal_ensure_locks.pop(session_id, None)
        _grok_terminal_ensure_locks.pop(session_id, None)
        _repl_terminal_ensure_locks.pop(session_id, None)
        # Close terminals with ``session.resource.deleted`` events BEFORE
        # cleanup_session — cleanup_conversation would silently pop them
        # from the registry, leaving clients showing a dead terminal
        # whose attach fails with "terminal resource not found".
        await _teardown_session_terminals(session_id)
        await resource_registry.cleanup_session(session_id)
        _session_spec_cache.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_tool_schemas.pop(session_id, None)
        _compaction_contexts.pop(session_id, None)
        _session_snapshot_cache.pop(session_id, None)
        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "object": "session.state_reset",
                "reset": True,
            },
        )

