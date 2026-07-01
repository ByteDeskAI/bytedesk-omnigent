    @app.delete("/v1/sessions/{session_id}/resources")
    async def cleanup_session_resources(
        session_id: str,
    ) -> JSONResponse:
        """Close all resources owned by a session.

        Runner-internal endpoint invoked by session/conversation
        deletion.  Closes the primary OSEnv, terminals, and removes
        registry entries.  Preserves workspace files for post-mortem.

        :param session_id: Session/conversation identifier.
        :returns: Confirmation with cleanup status.
        """
        _codex_terminal_ensure_locks.pop(session_id, None)
        _claude_terminal_ensure_locks.pop(session_id, None)
        _pi_terminal_ensure_locks.pop(session_id, None)
        _grok_terminal_ensure_locks.pop(session_id, None)
        _repl_terminal_ensure_locks.pop(session_id, None)
        await resource_registry.cleanup_session(session_id)
        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "object": "session.resources.cleaned",
                "cleaned": True,
            },
        )

