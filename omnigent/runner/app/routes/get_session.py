    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> JSONResponse:
        """
        Return the runner-local status of a session.

        The server calls this to derive session status. Fields
        not owned by the runner (``title``, ``labels``, etc.)
        return their defaults; the server overlays its own values.

        Per ``designs/SESSION_REARCHITECTURE.md`` §4 step 3.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: :class:`SessionResponse`-shaped JSON; 404 if
            no harness subprocess is registered.
        """
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": ("Runner GET /v1/sessions/{id} needs a HarnessProcessManager."),
                },
            )
        if not process_manager.has_session(session_id):
            return JSONResponse(
                status_code=404,
                content={
                    "error": "not_found",
                    "detail": (f"No session '{session_id}' on this runner."),
                },
            )
        has_turn = session_id in _active_turns or process_manager.has_active_turn(session_id)
        status = "running" if has_turn else "idle"
        agent_id = _session_agent_ids.get(session_id)
        if agent_id is None:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "detail": (
                        f"Session '{session_id}' registered but agent_id missing from cache."
                    ),
                },
            )
        created_at = _session_start_cache.get(session_id)
        if created_at is None:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "detail": (
                        f"Session '{session_id}' registered but start_time missing from cache."
                    ),
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "id": session_id,
                "agent_id": agent_id,
                "status": status,
                "created_at": int(created_at),
                "title": None,
                "labels": {},
                "runner_id": None,
                "reasoning_effort": None,
                "items": [],
                "permission_level": None,
            },
        )

