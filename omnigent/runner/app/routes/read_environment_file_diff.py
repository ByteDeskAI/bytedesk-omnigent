    @app.get(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/diff/{relative_path:path}"
    )
    async def read_environment_file_diff(
        session_id: str,
        environment_id: str,
        relative_path: str,
    ) -> JSONResponse:
        """Return before/after diff content for a changed file.

        Looks up the pre-modification snapshot (seeded by the caller before
        each write or edit — REST handlers call ``seed_snapshot`` before
        writing; ``sys_os_write``/``sys_os_edit`` do the same) and the
        current file content, then returns both so the UI can render a
        before/after diff view.

        Returns ``404`` when *relative_path* is not in the changed-files
        registry (i.e. it was never modified or created this session).

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root,
            e.g. ``"src/foo.py"``.
        :returns: JSON with ``before`` and ``after`` content strings (either
            may be ``null``).
        """
        agent_spec = await _require_os_env(session_id)
        await _ensure_session_registered(session_id)
        session_registry = await _resolve_session_fs_registry(session_id)

        from omnigent.entities.environment_filesystem import InvalidPath
        from omnigent.runner.environment_filesystem import _validate_path

        try:
            relative_path = _validate_path(relative_path)
        except InvalidPath as exc:
            # InvalidPath is a 400 input-validation error with a
            # developer-authored, non-sensitive message (e.g. "Path traversal
            # is not allowed"). Surface it verbatim like the global
            # ResourceError handler does, rather than genericizing useful
            # client feedback — str(exc) here carries no server internals.
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_path",
                        "message": str(exc),
                    }
                },
            )
        if not relative_path:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_path",
                        "message": "Cannot diff the environment root",
                    }
                },
            )

        # Check the file is tracked in the changed-files registry.
        record = (
            session_registry.get_changed_file(session_id, relative_path)
            if session_registry is not None
            else None
        )
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (
                            f"Path {relative_path!r} is not in the "
                            "changed-files registry for this session"
                        ),
                    }
                },
            )
        is_deleted = record.get("status") == "deleted"

        # ``before``: pre-modification baseline — seeded snapshot (first-write-wins)
        # for sessions that called seed_snapshot, git HEAD for git workspaces,
        # None for new/untracked files.  Wrapped in asyncio.to_thread because
        # get_baseline may invoke a subprocess (git show).
        import asyncio as _asyncio

        before: str | None = (
            await _asyncio.to_thread(session_registry.get_baseline, relative_path)
            if session_registry is not None
            else None
        )

        # ``after``: current on-disk content via the sandbox, consistent with
        # the rest of the filesystem API.  Pass limit=None to bypass the
        # 2 000-line agent-tool cap — the diff view needs the full file.
        from omnigent.runner.environment_filesystem import CallerProcessFilesystem

        after: str | None = None
        if not is_deleted:
            env = resource_registry.resolve_environment(session_id, environment_id, agent_spec)
            fs = CallerProcessFilesystem(env)
            content = await fs.read(relative_path, limit=None)
            after = content.data.decode(content.encoding or "utf-8", errors="replace")

        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.file_diff",
                "path": relative_path,
                "before": before,
                "after": after,
            },
        )

