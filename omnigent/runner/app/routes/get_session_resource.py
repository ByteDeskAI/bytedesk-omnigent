    @app.get("/v1/sessions/{session_id}/resources/{resource_id}")
    async def get_session_resource(
        session_id: str,
        resource_id: str,
    ) -> JSONResponse:
        """Return a single resource by id from the unified inventory.

        :param session_id: Session/conversation identifier.
        :param resource_id: Opaque resource id.
        :returns: The resource object regardless of type.
        """
        resource = resource_registry.get_resource(
            session_id,
            resource_id,
        )
        if resource is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (f"Resource {resource_id!r} not found"),
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource),
        )

    # ── Phase 4: session resource cleanup endpoint ────────────────

