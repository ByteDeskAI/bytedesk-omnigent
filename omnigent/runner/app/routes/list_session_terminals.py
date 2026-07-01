    @app.get("/v1/sessions/{session_id}/resources/terminals")
    async def list_session_terminals(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        """Return only terminal resources for a session.

        :param session_id: Session/conversation identifier.
        :param limit: Max resources to return.
        :param after: Cursor resource id.
        :param before: Cursor resource id.
        :param order: Sort order.
        :returns: Filtered ``PaginatedList`` of terminal resources.
        """
        return _build_typed_list_response(
            session_id,
            "terminal",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

