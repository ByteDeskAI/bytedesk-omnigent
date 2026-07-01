    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/search")
    async def search_environment_files(
        session_id: str,
        environment_id: str,
        q: str = Query(min_length=1, pattern=r".*\S.*"),
        include: str | None = Query(default=None),
        exclude: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> JSONResponse:
        """Search for files recursively by name/path substring and glob filters.

        Walks the full directory tree in the session's OS environment and
        returns files matching ``q`` (a case-insensitive name/path substring),
        optionally scoped by glob filters: ``exclude`` globs drop files and
        ``include`` globs restrict which files are kept.  Glob patterns use the
        VSCode/Cursor subset (``*``, ``**``, ``?``, ``{a,b}``).  Only file
        entries are returned (not directories).  Results are capped at
        ``limit``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param environment_id: Environment resource id,
            e.g. ``"default"``.
        :param q: Case-insensitive search substring, e.g. ``"test.md"``.
            Must contain at least one non-whitespace character.
        :param include: Comma-separated glob patterns scoping which files are
            returned, e.g. ``"*.ts,src/**"``.
        :param exclude: Comma-separated glob patterns for files to drop,
            e.g. ``"**/node_modules,*.test.ts"``.
        :param limit: Maximum number of results (1-500, default 500).
        :returns: JSON list response with matching filesystem entries.
        """
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
            split_glob_list,
        )

        # Brace-aware split so "*.{js,ts}" stays one pattern (its inner comma
        # is not a list separator). split_glob_list handles None/blank.
        include_patterns = split_glob_list(include)
        exclude_patterns = split_glob_list(exclude)

        agent_spec = await _require_os_env(session_id)  # also resolves spec
        await _ensure_session_registered(session_id)
        env = resource_registry.resolve_environment(session_id, environment_id, agent_spec)
        fs = CallerProcessFilesystem(env)
        entries = await fs.search_files(
            q,
            include=include_patterns,
            exclude=exclude_patterns,
            limit=limit,
        )
        data = [_fs_entry_to_dict(e) for e in entries]
        return JSONResponse(
            status_code=200,
            content={"object": "list", "data": data, "has_more": len(entries) >= limit},
        )

