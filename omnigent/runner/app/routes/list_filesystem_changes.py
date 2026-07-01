    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/changes")
    async def list_filesystem_changes(
        session_id: str,
        environment_id: str,  # noqa: ARG001
    ) -> JSONResponse:
        """List changed files for the session (flat, registry-backed).

        Returns a flat list of files that the agent has created, modified,
        or deleted, regardless of directory depth.  Behavior is
        mode-dependent:

        - **Non-git workspaces** (``AgentEditFilesystemRegistry``): returns
          only files touched by the agent via ``sys_os_write``,
          ``sys_os_edit``, or the REST write/edit/delete filesystem
          endpoints during this session.  Shell tool (``sys_os_shell``)
          side-effects are not tracked.  No background watcher is involved.
        - **Git workspaces** (``GitFilesystemRegistry``): returns all files
          with uncommitted changes in the working tree (``git status``),
          regardless of which session wrote them.  Session-scoped filtering
          is not available in git mode.

        This endpoint is distinct from the directory listing endpoint
        (``GET /filesystem``) which reflects the current on-disk state.
        Use this endpoint for the flat "changed files" view; use the
        directory listing endpoints for hierarchical browsing.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param environment_id: Environment resource id,
            e.g. ``"default"``.
        :returns: JSON list of changed file entries with ``status`` field.
        """
        await _require_os_env(session_id)
        await _ensure_session_registered(session_id)
        session_registry = await _resolve_session_fs_registry(session_id)
        raw_changes = (
            session_registry.list_changed_files(
                session_id,
                limit=10_000,
            )
            if session_registry is not None
            else []
        )
        data = [
            {
                "object": "session.environment.filesystem.entry",
                "path": rec["path"],
                "name": rec["path"].split("/")[-1],
                "status": rec["status"],
                "bytes": rec.get("bytes"),
                "modified_at": rec.get("modified_at"),
            }
            for rec in raw_changes
        ]
        return JSONResponse(
            status_code=200,
            content={"object": "list", "data": data, "has_more": False},
        )

