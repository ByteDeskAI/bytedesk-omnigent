    @app.get(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def read_or_list_environment_path(
        session_id: str,
        environment_id: str,
        relative_path: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        """Read a file or list a directory in an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param limit: Max entries for directory listing.
        :param after: Cursor entry id.
        :param before: Cursor entry id.
        :param order: Sort order.
        :returns: File content or directory listing.
        """
        await _require_os_env(session_id)
        return await _fs_list_or_read(
            session_id,
            environment_id,
            relative_path,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

