    @app.put(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def write_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> JSONResponse:
        """Write/replace a file in an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param request: JSON body with ``content`` and optional
            ``encoding`` and ``create_parents``.
        :returns: Write result with change tracking.
        """
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
        body = await request.json()
        content_str = body.get("content", "")
        encoding = body.get("encoding", "utf-8")
        create_parents = body.get("create_parents", True)
        content_bytes = content_str.encode(encoding)
        # Seed the diff snapshot with the current content *before* overwriting
        # so the diff endpoint can return the true pre-modification state.
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
        result = await fs.write(
            relative_path,
            content_bytes,
            create_parents=create_parents,
        )
        if filesystem_registry is not None:
            filesystem_registry.record_change(relative_path, result.operation, session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.write_result",
                "operation": result.operation,
                "path": result.path,
                "created": result.created,
                "bytes_written": result.bytes_written,
                "entry": _fs_entry_to_dict(result.entry) if result.entry else None,
            },
        )

