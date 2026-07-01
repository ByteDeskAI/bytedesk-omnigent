    @app.delete(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def delete_environment_path(
        session_id: str,
        environment_id: str,
        relative_path: str,
        recursive: bool = Query(default=False),
    ) -> JSONResponse:
        """Delete a file or directory in an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param recursive: Allow recursive directory deletion.
        :returns: Delete result.
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
        result = await fs.delete(relative_path, recursive=recursive)
        if filesystem_registry is not None and result.type == "file":
            filesystem_registry.record_change(relative_path, "deleted", session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.delete_result",
                "operation": result.operation,
                "path": result.path,
                "deleted": result.deleted,
                "type": result.type,
                "bytes_deleted": result.bytes_deleted,
                "entries_deleted": result.entries_deleted,
            },
        )

    async def _ensure_session_registered(session_id: str) -> None:
        """Cache the session's created_at and workspace to avoid repeated server fetches.

        Reads the shared :func:`_session_snapshot` (one
        ``GET /v1/sessions/{id}`` per session) on first access and
        projects ``created_at`` + ``workspace`` into their caches.
        Subsequent calls for the same session_id short-circuit
        immediately.  ``created_at`` falls back to the current wall time
        when the snapshot fetch fails.

        The ``workspace`` field may differ from the runner's global
        ``runner_workspace`` when the session uses a git worktree.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: None.
        """
        if session_id in _session_start_cache:
            return
        snapshot = await _session_snapshot(session_id)
        _session_start_cache[session_id] = snapshot.created_at
        _session_workspace_cache[session_id] = snapshot.workspace

    async def _resolve_session_spec_entry(session_id: str) -> Any | None:
        """
        Resolve the session-scoped spec *entry*, populating the cache.

        Returns the entry (a :class:`ResolvedSpec` or bare spec) rather
        than the unwrapped spec, so callers that need the materialized
        bundle workdir — e.g. skill discovery — can read it via
        :func:`_resolved_spec_workdir`. Resource access can happen
        before the first turn dispatches, so the harness process
        manager may not have loaded the session's spec yet; this reads
        the shared :func:`_session_snapshot` for the session's
        ``agent_id`` and reuses the normal ``spec_resolver`` path.

        A per-session lock makes resolution single-flight: a startup
        burst of concurrent callers resolves the bundle once and the
        rest read the cached entry, instead of each issuing its own
        ``agent/contents`` fetch. The success cache is keyed on the
        resolved entry only — failures are re-raised without caching so
        the next call retries once the agent binds to the session.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The cached/resolved spec entry, or ``None`` when no
            spec resolver is configured for this runner.
        :raises OmnigentError: If the server returns malformed data
            or the referenced agent cannot be resolved.
        """
        if session_id in _session_spec_cache:
            return _session_spec_cache[session_id]
        if spec_resolver is None:
            _session_spec_cache[session_id] = None
            return None
        lock = _session_spec_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # Re-check under the lock: a concurrent caller may have
            # resolved the spec while we waited to acquire it.
            if session_id in _session_spec_cache:
                return _session_spec_cache[session_id]
            snapshot = await _session_snapshot(session_id)
            if not snapshot.ok:
                raise OmnigentError(
                    f"session spec resolver: GET /v1/sessions/{session_id} "
                    f"failed with HTTP {snapshot.status_code}",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            agent_id = snapshot.agent_id
            if not agent_id:
                raise OmnigentError(
                    f"session spec resolver: session {session_id!r} has no agent_id",
                    code=ErrorCode.NOT_FOUND,
                )
            spec_entry = await spec_resolver(agent_id, session_id)
            if spec_entry is None:
                raise OmnigentError(
                    f"session spec resolver: agent {agent_id!r} for "
                    f"session {session_id!r} was not found",
                    code=ErrorCode.NOT_FOUND,
                )
            # Sub-agent swap: the bound agent_id resolves to the PARENT
            # spec, so cache the child's sub-spec for a sub-agent session.
            # Otherwise _session_spec_cache (and _session_harness_name /
            # _is_native_harness, which read it) report the parent harness —
            # the misclassification that respawns a claude-native sub-agent
            # as claude-sdk and tears down its terminal ("Bridge closed").
            # The snapshot carries sub_agent_name; backfill the in-memory map
            # so the dispatch-path swap is cheap too.
            sub_agent_name = snapshot.sub_agent_name
            if sub_agent_name:
                _session_sub_agent_names[session_id] = sub_agent_name
                from omnigent.runtime.workflow import _find_spec_by_name

                parent_spec = _unwrap_resolved_spec(spec_entry)
                if parent_spec is not None:
                    sub_spec = _find_spec_by_name(parent_spec, sub_agent_name)
                    if sub_spec is not None:
                        workdir = _resolved_spec_workdir(spec_entry)
                        spec_entry = (
                            ResolvedSpec(spec=sub_spec, workdir=workdir)
                            if workdir is not None
                            else sub_spec
                        )
            _session_spec_cache[session_id] = spec_entry
            return spec_entry

    async def _resolve_session_agent_spec(session_id: str) -> Any | None:
        """
        Resolve the session-scoped agent spec for filesystem resources.

        Thin wrapper over :func:`_resolve_session_spec_entry` that
        returns the unwrapped spec, so primary OS environment creation
        honors the uploaded bundle's ``os_env`` settings.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The parsed session agent spec, or ``None`` when
            no spec resolver is configured.
        :raises OmnigentError: If the server returns malformed data
            or the referenced agent cannot be resolved.
        """
        entry = await _resolve_session_spec_entry(session_id)
        return _unwrap_resolved_spec(entry) if entry is not None else None

    async def _resolve_session_skills(session_id: str) -> list[SkillSpec]:
        """
        Resolve the merged (bundled + host) skills for a session.

        Skills are runner-owned and combine every source the agent can
        load, discovered against *this runner's* filesystem and honoring
        the spec's ``skills_filter``:

        * the spec's bundled ``skills`` (the bundle's ``skills/`` dir);
        * host skills under the **session's workspace** — the agent's
          working directory on this runner (the claude-native TUI's cwd,
          the in-process harness workspace, a git worktree), where a
          project's ``.claude/skills/`` live;
        * host skills under the **agent bundle workdir**;
        * user-global host skills (``~/.claude/skills`` etc., scanned by
          :func:`discover_host_skills`).

        The workspace is the primary root because that is where the
        harness actually loads project skills; the bundle workdir is
        unioned in for completeness (it is a throwaway temp dir for
        single-YAML agents like ``claude-native-ui``, so usually
        contributes nothing). Falls back to the runner's global workspace,
        then the process cwd, when no workspace is known. Deduplicated by
        name with bundled winning, then earlier roots winning. Cached per
        session so the filesystem walk runs once per session lifetime
        (dropped in ``delete_session``).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: Bundled skills followed by host skills, deduplicated
            by name. Empty when no spec resolver is configured or the
            spec exposes no skills.
        :raises OmnigentError: If the session's spec cannot be
            resolved.
        """
        cached = _session_skills_cache.get(session_id)
        if cached is not None:
            return cached
        entry = await _resolve_session_spec_entry(session_id)
        spec = _unwrap_resolved_spec(entry) if entry is not None else None
        if spec is None:
            return []
        workspace = await _session_workspace_value(session_id)
        # Host-discovery roots in priority order: the session workspace
        # (where the harness runs) first, then the agent bundle workdir.
        # Both are unioned; ``discover_host_skills`` also scans ``~`` on
        # each call. Distinct, resolved, non-None paths only.
        candidate_roots = [
            Path(workspace).resolve()
            if workspace is not None
            else (runner_workspace.resolve() if runner_workspace is not None else None),
            _resolved_spec_workdir(entry),
        ]
        roots: list[Path] = []
        for candidate in candidate_roots:
            if candidate is None:
                continue
            resolved = candidate.resolve()
            if resolved not in roots:
                roots.append(resolved)
        # No workspace and no bundle workdir: match the cwd fallback the
        # in-process LoadSkillTool uses so behavior stays consistent.
        if not roots:
            roots.append(Path.cwd())

        def _discover() -> list[SkillSpec]:
            """Merge bundled + host skills (every root) off the event loop."""
            merged: list[SkillSpec] = list(spec.skills)
            seen = {s.name for s in merged}
            for root in roots:
                for hs in discover_host_skills(root, spec.skills_filter):
                    if hs.name not in seen:
                        seen.add(hs.name)
                        merged.append(hs)
            return merged

        skills = await asyncio.to_thread(_discover)
        _session_skills_cache[session_id] = skills
        return skills

