    @app.get("/v1/sessions/{session_id}/skills")
    async def get_session_skills(session_id: str) -> JSONResponse:
        """
        Return the merged (bundled + host) skills for a session.

        Skills are runner-owned: discovery walks *this* runner's
        filesystem (the materialized bundle and the runner's
        ``~/.claude/skills/``), not the Omnigent server's. The server overlays
        this list onto the session snapshot it serves to clients (the
        web composer's slash-command menu).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: JSON ``{"skills": [{"name", "description"}, ...]}``.
            Empty list when the runner has no spec resolver wired.
        """
        skills = await _resolve_session_skills(session_id)
        return JSONResponse(
            status_code=200,
            content={"skills": [{"name": s.name, "description": s.description} for s in skills]},
        )

