    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/filesystem")
    async def list_environment_root(
        session_id: str,
        environment_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        """List the root directory of an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param limit: Max entries to return.
        :param after: Cursor entry id.
        :param before: Cursor entry id.
        :param order: Sort order.
        :returns: PaginatedList of filesystem entries.
        """
        await _require_os_env(session_id)
        return await _fs_list_or_read(
            session_id,
            environment_id,
            "",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

