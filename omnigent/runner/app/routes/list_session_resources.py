    @app.get("/v1/sessions/{session_id}/resources")
    async def list_session_resources(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        type: str | None = Query(default=None),
    ) -> JSONResponse:
        """Runner-side session resource inventory.

        :param session_id: Session/conversation identifier.
        :param limit: Max resources to return, default 20.
        :param after: Cursor resource id for forward pagination.
        :param before: Cursor resource id for backward pagination.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :param type: Optional resource-type filter.
        :returns: PaginatedList of session resources.
        """
        from omnigent.entities.pagination import paginate_in_memory

        spec = await _resolve_session_agent_spec(session_id)
        full = resource_registry.list_resources(
            session_id,
            resource_type=type,
            agent_spec=spec,
        )
        page = paginate_in_memory(
            full.data,
            id_fn=lambda r: r.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [session_resource_view_to_dict(r) for r in page.data]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "data": data,
                "first_id": page.first_id,
                "last_id": page.last_id,
                "has_more": page.has_more,
            },
        )

    # ── Phase 1b: typed resource collections ───────────────────
    # Register typed collection routes BEFORE /{resource_id} so
    # names like "terminals" and "environments" are never captured
    # as resource ids.

    def _build_typed_list_response(
        session_id: str,
        resource_type: str,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> JSONResponse:
        """Build a PaginatedList response filtered by resource type.

        :param session_id: Session/conversation identifier.
        :param resource_type: One of ``"environment"``,
            ``"terminal"``, or ``"file"``.
        :param limit: Max resources to return.
        :param after: Cursor resource id.
        :param before: Cursor resource id.
        :param order: Sort order.
        :returns: JSON response with filtered resource list.
        """
        from omnigent.entities.pagination import paginate_in_memory

        filtered = resource_registry.list_resources(
            session_id,
            resource_type=resource_type,
        )
        page = paginate_in_memory(
            filtered.data,
            id_fn=lambda r: r.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [session_resource_view_to_dict(r) for r in page.data]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "data": data,
                "first_id": page.first_id,
                "last_id": page.last_id,
                "has_more": page.has_more,
            },
        )

