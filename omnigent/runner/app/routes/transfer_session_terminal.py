    @app.post("/v1/sessions/{session_id}/resources/terminals/{terminal_id}/transfer")
    async def transfer_session_terminal(
        session_id: str,
        terminal_id: str,
        request: Request,
    ) -> JSONResponse:
        """Move a terminal resource to another session without closing it.

        This runner-local endpoint does not perform user/session ACL
        checks: the runner has no Omnigent permission store. Public callers
        must use the Omnigent session-resource transfer route, which validates
        edit access on both source and target sessions before proxying
        this request to the bound runner. The runner validates only its
        local invariant: the terminal must still belong to
        ``session_id`` before it can be reparented.

        :param session_id: Current owning session/conversation id.
        :param terminal_id: Opaque terminal resource id,
            e.g. ``"terminal_claude_main"``.
        :param request: JSON body containing ``target_session_id``.
        :returns: The terminal resource object projected under the
            target session.
        """
        body = await request.json()
        target_session_id = body.get("target_session_id") if isinstance(body, dict) else None
        if not isinstance(target_session_id, str) or not target_session_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'target_session_id' is required",
                    }
                },
            )
        try:
            resource = await resource_registry.transfer_terminal(
                source_session_id=session_id,
                target_session_id=target_session_id,
                terminal_id=terminal_id,
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "resource_conflict",
                        "message": _client_safe_error_detail(exc, context="terminal transfer"),
                    }
                },
            )
        if resource is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"Terminal {terminal_id!r} not found",
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource),
        )

