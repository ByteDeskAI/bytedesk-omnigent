    @app.get("/v1/sessions/{session_id}/resources/terminals/{terminal_id}")
    async def get_session_terminal(
        session_id: str,
        terminal_id: str,
    ) -> JSONResponse:
        """Return a single terminal resource by id.

        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id,
            e.g. ``"terminal_bash_s1"``.
        :returns: The terminal resource object.
        """
        resource = await resource_registry.get_terminal_resource(
            session_id,
            terminal_id,
        )
        if resource is None:
            _log_terminal_lookup_miss(resource_registry, session_id, terminal_id)
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
            content=session_resource_view_to_dict(resource),
        )

