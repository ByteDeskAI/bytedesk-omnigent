    @app.post("/v1/sessions/{session_id}/skills/resolve")
    async def resolve_session_skill(session_id: str, request: Request) -> JSONResponse:
        """
        Resolve a skill invocation into its hidden ``<skill>`` meta text.

        The runner owns the skill's on-disk content: it reads the
        ``SKILL.md`` body and lists resource files from the skill's
        directory *on this runner*, so the embedded ``<path>`` and
        resource listing match what the ``read_skill_file`` tool
        resolves at runtime. The Omnigent server calls this, persists the
        returned text as a hidden meta item, and forwards it as the turn
        input (runner-resolves, server-persists).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param request: Request whose JSON body carries ``{"name": str,
            "arguments": str}`` — the skill name and the raw argument
            string typed after the slash command (``arguments`` defaults
            to ``""``).
        :returns: JSON ``{"meta_text": str}`` on success; 404
            ``{"error": "skill_not_found", "detail": str, "available":
            [str, ...]}`` when the skill is not exposed for this session;
            400 when the body is not a JSON object, ``name`` is missing,
            or ``arguments`` is not a string.
        """
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "Request body must be JSON."},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "Request body must be a JSON object.",
                },
            )
        name = body.get("name")
        arguments = body.get("arguments", "")
        if not isinstance(name, str) or not name:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "'name' is required."},
            )
        if not isinstance(arguments, str):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "'arguments' must be a string."},
            )
        skills = await _resolve_session_skills(session_id)
        skill = find_skill_by_name(skills, name)
        if skill is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "skill_not_found",
                    "detail": (f"Skill {name!r} not found for session {session_id!r}."),
                    "available": sorted(s.name for s in skills),
                },
            )
        return JSONResponse(
            status_code=200,
            content={"meta_text": format_skill_meta_text(skill, arguments)},
        )

    async def _fs_list_or_read(
        session_id: str,
        environment_id: str,
        path: str,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> JSONResponse:
        """Dispatch GET to list_dir or read depending on path type.

        For file paths the response includes a ``content_type`` field
        derived from ``mimetypes.guess_type`` (per the
        UI_SESSION_RESOURCES_MIGRATION design).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param environment_id: Environment resource id,
            e.g. ``"default"``.
        :param path: Relative path (empty string for root).
        :param limit: Max entries for directory listing.
        :param after: Cursor entry id for forward pagination.
        :param before: Cursor entry id for backward pagination.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: JSON response with directory listing or file content.
        """
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        await _ensure_session_registered(session_id)
        agent_spec = await _resolve_session_agent_spec(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )

        fs = CallerProcessFilesystem(env)
        resolved = fs._resolve(path)

        if resolved.is_dir():
            page = await fs.list_dir(
                path,
                limit=limit,
                after=after,
                before=before,
                order=order,
            )
            data = [_fs_entry_to_dict(e) for e in page.data]
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

        content = await fs.read(path)
        # Derive MIME type from the file path for syntax highlighting
        # and binary-vs-text rendering in UI clients.
        content_type_guess, _ = mimetypes.guess_type(path)
        payload: dict[str, object] = {
            "object": "session.environment.filesystem.file_content",
            "path": content.path,
            "content_type": content_type_guess,
            "bytes": content.bytes,
            "truncated": content.truncated,
        }
        if content.encoding:
            payload["encoding"] = content.encoding
            payload["content"] = content.data.decode(content.encoding)
        else:
            import base64

            payload["encoding"] = "base64"
            payload["content"] = base64.b64encode(content.data).decode()
        return JSONResponse(status_code=200, content=payload)

    def _fs_entry_to_dict(entry: FilesystemEntry) -> dict[str, object]:
        """Convert a FilesystemEntry to a JSON-serializable dict.

        :param entry: The filesystem entry.
        :returns: Dict matching the API shape.
        """
        return {
            "id": entry.id,
            "object": "session.environment.filesystem.entry",
            "name": entry.name,
            "path": entry.path,
            "type": entry.type,
            "bytes": entry.bytes,
            "modified_at": entry.modified_at,
        }

    # ── Phase 5: environment shell endpoint ────────────────────────

