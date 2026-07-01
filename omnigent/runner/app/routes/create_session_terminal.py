    @app.post("/v1/sessions/{session_id}/resources/terminals")
    async def create_session_terminal(
        session_id: str,
        request: Request,
    ) -> JSONResponse:
        """Launch or return an existing terminal resource.

        Preserves the idempotency semantics of ``sys_terminal_launch``:
        creating an already-running ``(terminal, session_key)`` returns
        the existing resource rather than spawning a duplicate.

        :param session_id: Session/conversation identifier.
        :param request: JSON body with ``terminal`` and ``session_key``.
        :returns: The terminal resource object.
        """
        body = await request.json()
        terminal_name = body.get("terminal")
        session_key = body.get("session_key")
        if not terminal_name or not session_key:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": ("'terminal' and 'session_key' are required"),
                    }
                },
            )

        # Resume "ensure" path (see _ensure_claude_terminal_on_runner): the CLI
        # marks the request with ``ensure_native_terminal`` to ask for the full
        # claude-native setup that only _auto_create_claude_terminal does (incl.
        # cold resume); the generic launch below can't reproduce it. Keyed on
        # the explicit marker — NOT on the absence of spec/bridge_inject_dir,
        # which is ambiguous with a plain generic claude launch. Idempotent:
        # return the live terminal if present, else auto-create.
        if (
            body.get("ensure_native_terminal")
            and terminal_name == "claude"
            and session_key == "main"
        ):
            claude_terminal_id = terminal_resource_id("claude", "main")
            # Serialize the ensure check-and-create with _claude_terminal_ensure_locks
            # so concurrent calls from _on_runner_connect (create_session) and the
            # message path's _ensure_native_terminal_ready (here) cannot both find no
            # terminal and both call _auto_create_claude_terminal — which spawns two
            # forwarders and double-persists every transcript item.
            _ensure_lock = _claude_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, claude_terminal_id
                )
                if existing is not None:
                    _logger.info(
                        "Claude terminal ensure returning existing resource: session=%s "
                        "terminal_id=%s",
                        session_id,
                        claude_terminal_id,
                    )
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                _logger.info(
                    "Claude terminal ensure auto-creating missing resource: session=%s "
                    "terminal_id=%s",
                    session_id,
                    claude_terminal_id,
                )
                try:
                    terminal_view = await _auto_create_claude_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Claude terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Claude")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )
        if (
            body.get("ensure_native_terminal")
            and terminal_name == "codex"
            and session_key == "main"
        ):
            codex_terminal_id = terminal_resource_id("codex", "main")
            ensure_lock = _codex_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, codex_terminal_id
                )
                if existing is not None:
                    if _is_runner_owned_codex_terminal(resource_registry, existing):
                        return _codex_ensure_response_with_policy_notice(session_id, existing)
                    _logger.info(
                        "Replacing non-native codex terminal %s for session %s",
                        codex_terminal_id,
                        session_id,
                    )
                    closed = await resource_registry.close_terminal(session_id, codex_terminal_id)
                    if not closed:
                        return JSONResponse(
                            status_code=409,
                            content={
                                "error": {
                                    "code": "terminal_conflict",
                                    "message": (
                                        "Existing codex terminal is not a runner-owned "
                                        "Codex TUI and could not be closed."
                                    ),
                                }
                            },
                        )
                try:
                    codex_agent_spec = await _resolve_session_agent_spec(session_id)
                    terminal_view = await _auto_create_codex_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        agent_spec=codex_agent_spec,
                        server_client=server_client,
                        ensure_comment_relay=_ensure_comment_relay_started,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Codex terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Codex")
                # Surface the one-shot policy notice while still holding the
                # per-session ensure lock so the read-and-clear of
                # ``policy_notice_pending`` is serialized with the
                # existing-terminal path above — two concurrent ensures can
                # never both emit the banner.
                return _codex_ensure_response_with_policy_notice(session_id, terminal_view)

        if body.get("ensure_native_terminal") and terminal_name == "pi" and session_key == "main":
            pi_terminal_id = terminal_resource_id("pi", "main")
            ensure_lock = _pi_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, pi_terminal_id
                )
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                try:
                    terminal_view = await _auto_create_pi_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Pi terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Pi")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )

        if (
            body.get("ensure_native_terminal")
            and terminal_name == "grok"
            and session_key == "main"
        ):
            grok_terminal_id = terminal_resource_id("grok", "main")
            ensure_lock = _grok_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, grok_terminal_id
                )
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                try:
                    terminal_view = await _auto_create_grok_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Grok terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Grok")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )

        from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

        cwd_override = body.get("cwd")
        sandbox_override = body.get("sandbox")
        spec = body.get("spec") or {}

        # Resolve the agent spec once: we need it for both the
        # declared-terminal lookup and to thread the agent's
        # ``os_env`` (with its sandbox / egress_rules /
        # env_passthrough) through as the inheritance parent. Without
        # the latter, the previous implementation built a fresh
        # TerminalEnvSpec with no sandbox at all — every
        # REST-launched terminal ran completely outside the agent's
        # sandbox, regardless of YAML config.
        agent_spec = await _resolve_session_agent_spec(session_id)
        agent_os_env = getattr(agent_spec, "os_env", None) if agent_spec is not None else None

        # Prefer the operator-declared terminal spec when the agent
        # YAML declares one with this name (e.g. ``sandboxed_zsh``).
        # The body cannot then inject command/args/env/sandbox —
        # only the per-call cwd/sandbox overrides gated by the
        # spec's allow_* flags.
        declared_terminal = None
        if agent_spec is not None:
            terminals_map = getattr(agent_spec, "terminals", None) or {}
            declared_terminal = terminals_map.get(terminal_name)

        if declared_terminal is not None:
            env_spec = declared_terminal
            # Body's ``spec.cwd`` becomes a cwd_override (still
            # subject to the spec's allow_cwd_override gate and
            # the launch-time containment check).
            cwd_override = cwd_override or spec.get("cwd")
        else:
            # No matching terminal in the YAML: synthesise from the
            # body but inherit the agent's sandbox so we don't punch
            # a hole in the policy. The wrapper use case
            # (omnigent claude) lands here; the launched terminal
            # picks up the agent's sandbox/egress instead of running
            # completely unsandboxed.
            spec_cwd = spec.get("cwd")
            if spec_cwd is None or spec_cwd in (".", "./"):
                spec_cwd = resource_registry.compute_default_env_root(session_id, agent_spec)
            env_spec = TerminalEnvSpec(
                os_env=OSEnvSpec(
                    type=spec.get("os_env_type", "caller_process"),
                    cwd=spec_cwd,
                    # Inherit the agent's sandbox by reference;
                    # build_terminal_os_env_spec deep-clones it.
                    sandbox=(agent_os_env.sandbox if agent_os_env is not None else None),
                ),
                command=spec.get("command", "bash"),
                args=spec.get("args", []),
                env=spec.get("env", {}),
                scrollback=spec.get("scrollback", 10000),
                tmux_allow_passthrough=bool(spec.get("tmux_allow_passthrough", False)),
                tmux_start_on_attach=bool(spec.get("tmux_start_on_attach", False)),
            )
        # Opt-in: callers (e.g. the ``omnigent claude`` wrapper) can ask the
        # runner to publish the launched terminal's tmux socket + target into a
        # bridge directory on this host, and to expose the comment tools to
        # Claude Code. Any truthy value (including a legacy path string from
        # older callers) enables it; the destination is derived server-side
        # from session_id, never from the body.
        bridge_inject = bool(body.get("bridge_inject_dir"))
        bridge_id: str | None = None
        relay_existed = False
        if bridge_inject:
            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client,
                session_id=session_id,
            )
            # Start the comment-tool relay BEFORE spawning Claude so
            # tool_relay.json is on disk before Claude Code's first MCP
            # tools/list — eliminating the cold-launch race where the tools
            # would be absent until a best-effort tools-changed notification.
            # The client already reset the bridge dir (prepare_bridge_dir wipes
            # tool_relay.json) before this request, so writing here is safe.
            relay_existed = session_id in _session_comment_relays
            await _ensure_comment_relay_started(session_id, bridge_id=bridge_id)

        try:
            launch_method = (
                resource_registry.launch_required_terminal
                if bridge_inject
                else resource_registry.launch_auxiliary_terminal
            )
            resource_view = await launch_method(
                session_id=session_id,
                terminal_name=terminal_name,
                session_key=session_key,
                spec=env_spec,
                cwd_override=cwd_override,
                sandbox_override=sandbox_override,
                parent_os_env=agent_os_env,
                # The bridge-inject path is the ``omnigent claude``
                # wrapper launching the claude-native agent terminal —
                # mark it so its pane activity drives the session's
                # PTY-derived working status.
                resource_role=(CLAUDE_NATIVE_TERMINAL_ROLE if bridge_inject else None),
            )
        except RuntimeError as exc:
            # The relay was started before the spawn; tear down any relay this
            # request started so a failed launch does not leak a bound socket or
            # a stale advertisement. ``relay_existed`` guards against closing a
            # relay a prior launch owns (idempotent re-launch).
            if bridge_inject and not relay_existed:
                relay = _session_comment_relays.pop(session_id, None)
                if relay is not None:
                    relay.close()
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "terminal_launch_failed",
                        "message": _client_safe_error_detail(exc, context="terminal launch"),
                    }
                },
            )

        if bridge_inject:
            # Publish the launched terminal's tmux target now that the pane
            # exists (the publish needs the spawned terminal).
            _publish_tmux_target_for_bridge(
                resource_registry=resource_registry,
                session_id=session_id,
                bridge_id=bridge_id,
                terminal_name=terminal_name,
                session_key=session_key,
            )

        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource_view),
        )

