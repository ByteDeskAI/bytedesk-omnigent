    @app.patch(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def edit_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> JSONResponse:
        """Edit a file in an environment via text replacement.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param request: JSON body with ``old_text``, ``new_text``,
            and optional ``replace_all``.
        :returns: Edit result with change tracking.
        """
        from omnigent.entities.environment_filesystem import (
            TextEditRequest,
        )
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        fs = CallerProcessFilesystem(env)
        # Seed the diff snapshot with the current content *before* editing.
        try:
            existing = await fs.read(relative_path, limit=None)
            if existing.encoding and filesystem_registry is not None:
                filesystem_registry.seed_snapshot(
                    relative_path,
                    existing.data.decode(existing.encoding, errors="replace"),
                    session_id=session_id,
                )
        except Exception:  # noqa: BLE001
            pass
        body = await request.json()
        edit_req = TextEditRequest(
            old_text=body.get("old_text"),
            new_text=body.get("new_text"),
            replace_all=body.get("replace_all", False),
        )
        result = await fs.edit_text(relative_path, edit_req)
        if filesystem_registry is not None:
            filesystem_registry.record_change(relative_path, result.operation, session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.edit_result",
                "operation": result.operation,
                "path": result.path,
                "replacements": result.replacements,
                "bytes_before": result.bytes_before,
                "bytes_after": result.bytes_after,
                "entry": _fs_entry_to_dict(result.entry) if result.entry else None,
            },
        )

