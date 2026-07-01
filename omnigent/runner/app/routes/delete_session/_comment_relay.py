    async def _ensure_comment_relay_started(
        session_id: str,
        *,
        bridge_id: str | None = None,
        explicit_bridge_dir: Path | None = None,
        await_notify: bool = False,
    ) -> None:
        """
        Ensure the comment-tool relay is running for a ``claude-native`` session.

        Writes ``tool_relay.json`` into the session's bridge directory so the
        MCP bridge subprocess (running inside Claude Code) discovers and
        dispatches ``list_comments`` / ``update_comment``, then fires a
        ``notifications/tools/list_changed`` so a Claude Code instance that has
        already fetched its tool list re-fetches it.

        Idempotent and session-scoped: the relay is started once and lives
        until the session is deleted (see the cleanup in ``delete_session``).
        It is started from two places, whichever runs first:

        - ``create_session_terminal`` (the ``bridge_inject_dir`` branch), which
          fires as the Claude terminal launches — after the client has reset
          the bridge dir and before Claude Code's MCP client performs its
          initial ``tools/list``. This is the normal ``omnigent claude``
          path: the comment tools land on that first list with no notification
          race, so the notification is sent in the background (the bridge
          server is not up yet, and awaiting it would block the launch).
        - ``_run_turn_bg`` on the first turn, as a fallback for sessions whose
          terminal was launched outside the runner terminal route — including
          UI-launched terminals, which are never pre-warmed. Here Claude Code
          has already listed its tools, so the relayed tools land a beat late;
          the caller passes ``await_notify=False`` anyway, because a fresh
          UI-launched terminal's bridge has not published ``server.json`` yet
          and awaiting delivery would stall the turn ~15s on the readiness
          poll. The notification fires in the background instead.

        Relay-start failures are logged and swallowed: the relay is additive,
        and a failed socket bind or file write must never break the terminal
        launch or the turn that triggered it.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param bridge_id: Opaque bridge id resolved by the caller, e.g.
            ``"bridge_abc123"``. ``None`` resolves it from the session labels
            via :func:`_claude_native_bridge_id_for_session`.
        :param await_notify: When ``True``, await the
            ``notifications/tools/list_changed`` delivery before returning
            (warm-bridge fallback path); when ``False``, fire it in the
            background (cold-bridge terminal-launch path). Pass ``False``
            for codex-native: codex starts its MCP bridge server lazily (only
            once it runs the turn), so awaiting delivery on a fresh session
            blocks for ``post_tools_changed``'s full readiness timeout (~30s)
            before the turn is dispatched. ``tool_relay.json`` is already on
            disk by then, so codex's initial ``tools/list`` sees the relay
            tools without the notification.
        :returns: None.
        """
        # Fast path: a relay is already running for this session.
        if session_id in _session_comment_relays:
            return

        import json as _json

        from omnigent.claude_native_bridge import (
            ClaudeNativeToolRelay,
            bridge_dir_for_bridge_id,
            post_tools_changed,
            start_tool_relay,
        )
        from omnigent.runner.tool_dispatch import should_relay_tool_to_native
        from omnigent.tools.builtins.agents import (
            SysAgentDownloadTool,
            SysAgentGetTool,
            SysAgentListTool,
        )
        from omnigent.tools.builtins.list_comments import ListCommentsTool
        from omnigent.tools.builtins.os_env import (
            SysOsEditTool,
            SysOsReadTool,
            SysOsShellTool,
            SysOsWriteTool,
        )
        from omnigent.tools.builtins.spawn import (
            SysSessionGetHistoryTool,
            SysSessionGetInfoTool,
            SysSessionListTool,
        )
        from omnigent.tools.builtins.update_comment import UpdateCommentTool

        # Resolve the bridge dir. When an explicit bridge_dir is
        # provided (codex-native path), skip the claude-native bridge
        # id lookup entirely — the caller already resolved it.
        if explicit_bridge_dir is not None:
            bridge_dir = explicit_bridge_dir
        else:
            # Resolve the bridge id (the only await) BEFORE recording
            # anything, so the start→store section below runs
            # atomically: a concurrent delete or a second starter
            # can't interleave mid-setup and strand a relay.
            if bridge_id is None:
                bridge_id = await _claude_native_bridge_id_for_session(
                    server_client=server_client,
                    session_id=session_id,
                )

            # Re-check: another starter may have published the relay
            # during the await.
            if session_id in _session_comment_relays:
                return

            bridge_dir = bridge_dir_for_bridge_id(bridge_id or session_id)

        # Build flat tool schemas (name + description + parameters) for the
        # native relay. start_tool_relay normalises these via
        # _normalize_relay_tool_specs before writing tool_relay.json.
        #
        # claude-native / codex-native ignore the harness ``tools`` list, so
        # this relay is the ONLY tool surface reaching the real CLI — tools
        # added here override the bridge's static tools of the same name,
        # giving centralized policy evaluation on the Omnigent server. Two groups
        # are assembled:
        #
        # 1. The runner-/server-proxied builtin surface, derived from the
        #    session's own ToolManager plus ``should_relay_tool_to_native`` so
        #    the relayed set includes both framework-owned builtin families and
        #    spec-declared generic builtins (e.g. bytedesk_jira). The
        #    spec-dependent schemas (e.g. sys_session_send's named-mode
        #    ``agent`` enum, present only when the spec declares sub-agents;
        #    sys_terminal_*, present only when the spec declares ``terminals:``)
        #    exactly match what non-native harnesses receive via
        #    ``request.tools``.
        # 2. OS tools (``sys_os_*``), relayed unconditionally below to
        #    override the bridge's static (non-policy-enforced) versions —
        #    independent of the spec's ``os_env`` gate.
        relay_schemas: list[dict[str, Any]] = []

        def _append_flat_schema(function_dict: dict[str, Any]) -> None:
            """
            Append a tool's OpenAI ``function`` schema in flat relay shape.

            :param function_dict: The ``"function"`` sub-dict of a tool
                schema, e.g. ``{"name": "sys_session_list", "parameters":
                {...}}``.
            :returns: None.
            """
            relay_schemas.append(
                {
                    "name": function_dict["name"],
                    "description": function_dict.get("description", ""),
                    "parameters": function_dict.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                }
            )

        # Resolve the session's agent spec so the relayed builtin surface
        # mirrors the spec's gating exactly. This is an await, so re-check
        # for a concurrently-started relay afterward. The relay is additive
        # and must never break the launch/turn, so a resolver error (HTTP
        # failure, not-yet-bound agent on a cold terminal launch) falls back
        # to the always-on read/discovery surface rather than propagating.
        try:
            relay_spec = await _resolve_session_agent_spec(session_id)
        except OmnigentError:
            relay_spec = None
        if session_id in _session_comment_relays:
            return
        if relay_spec is not None:
            from omnigent.tools.manager import ToolManager

            for _schema in ToolManager(relay_spec).get_tool_schemas():
                _fn = _schema["function"]
                if should_relay_tool_to_native(_fn["name"], relay_spec):
                    _append_flat_schema(_fn)
        else:
            # No resolvable spec: fall back to the always-on read/discovery
            # surface — never the opt-in spawn writes (send/close/create),
            # whose gate (``tools.agents`` or ``spawn: true``) can't be
            # evaluated without the spec.
            from omnigent.tools.builtins.policy import SysAddPolicyTool, SysPolicyRegistryTool

            for _cls in (
                ListCommentsTool,
                UpdateCommentTool,
                SysSessionListTool,
                SysSessionGetHistoryTool,
                SysSessionGetInfoTool,
                SysAgentGetTool,
                SysAgentListTool,
                SysAgentDownloadTool,
                SysAddPolicyTool,
                SysPolicyRegistryTool,
            ):
                _append_flat_schema(_cls().get_schema()["function"])

        # Add OS tool schemas. Create a minimal OSEnvironment for schema extraction.
        from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
        from omnigent.inner.os_env import create_os_environment

        _os_spec = OSEnvSpec(
            type="caller_process",
            cwd=str(Path.cwd()),
            sandbox=OSEnvSandboxSpec(type="none"),
            fork=False,
        )
        try:
            _os_env = create_os_environment(_os_spec)
            for _tool in (
                SysOsReadTool(_os_env),
                SysOsWriteTool(_os_env),
                SysOsEditTool(_os_env),
                SysOsShellTool(_os_env),
            ):
                _append_flat_schema(_tool.get_schema()["function"])
            _os_env.close()
        except Exception:  # noqa: BLE001
            # OS environment setup failed; relay will run without OS tools.
            # This should not happen in practice, but we log and continue
            # since the relay is additive.
            _logger.debug(
                "Could not create OSEnvironment for relay OS tool schemas; "
                "OS tools will not be available in relay for session=%s",
                session_id,
            )

        # Capture session_id in the closure so concurrent sessions are
        # routed correctly.
        _captured_session_id = session_id

        async def _relay_tool_executor(
            name: str,
            arguments: dict[str, Any],
        ) -> dict[str, Any]:
            """
            Relay one MCP tool call through the Omnigent server's /mcp endpoint.

            Routes the call through
            :class:`~omnigent.runner.proxy_mcp_manager.ProxyMcpManager`
            so the Omnigent server evaluates TOOL_CALL and TOOL_RESULT policies
            before executing the tool — consistent with all other harnesses
            (claude-sdk, openai-agents). Works for all relay tool types:
            comment tools, session query tools, and OS tools.

            :param name: Tool name, e.g. ``"list_comments"``,
                ``"sys_session_get_history"``, or ``"sys_os_read"``.
            :param arguments: Decoded tool arguments from Claude Code, e.g.
                ``{"conversation_id": "conv_abc"}`` or ``{"path": "file.txt"}``.
            :returns: Parsed JSON result dict for
                :func:`_mcp_response_from_tool_result`, e.g.
                ``{"items": [...]}`` or ``{"error": "..."}``.
            """
            result_str = await ProxyMcpManager(
                _captured_session_id, server_client, publish_event=_publish_event
            ).call_tool(None, name, arguments)
            try:
                return _json.loads(result_str)
            except _json.JSONDecodeError:
                # ProxyMcpManager returns raw text (not JSON) for
                # plain-text tool results (the MCP text-block content
                # joined as a string). Wrap it so
                # _mcp_response_from_tool_result receives a dict; the
                # "result" key is the same wrapper it would apply for
                # a non-dict value.
                return {"result": result_str}

        # start_tool_relay is synchronous, so start→store has no await: atomic.
        try:
            relay: ClaudeNativeToolRelay = start_tool_relay(
                bridge_dir=bridge_dir,
                tools=relay_schemas,
                tool_executor=_relay_tool_executor,
                loop=asyncio.get_running_loop(),
            )
        except (OSError, RuntimeError):
            # Relay is additive: a failed bind/write/thread-start must not break
            # the launch or turn. Nothing was recorded, so a later turn retries.
            _logger.warning(
                "Failed to start comment relay for session=%s",
                session_id,
                exc_info=True,
            )
            return
        _session_comment_relays[session_id] = relay

        async def _notify_tools_changed() -> None:
            """
            Notify Claude Code that its MCP tool list changed.

            ``post_tools_changed`` is synchronous and blocks until the bridge
            server publishes ``server.json``; run it in the default executor so
            the event loop is not blocked, and ignore the not-yet-ready bridge
            (the relay file is already on disk for the initial ``tools/list``).

            :returns: None.
            """
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, post_tools_changed, bridge_dir
                )
            except RuntimeError:
                _logger.debug(
                    "tools-changed notification skipped for session=%s (bridge server not ready)",
                    session_id,
                )

        if await_notify:
            # Warm-bridge fallback: the bridge is already up, so this returns
            # quickly and guarantees delivery before the caller injects the
            # user message — without a fixed sleep.
            await _notify_tools_changed()
        else:
            # Cold-bridge terminal-launch path: awaiting post_tools_changed
            # would block on its readiness wait. The relay file is already on
            # disk for Claude's initial tools/list, so notify in the background
            # purely to cover a warm re-attach.
            _notify_task = asyncio.create_task(_notify_tools_changed())
            _background_tasks.add(_notify_task)
            _notify_task.add_done_callback(_background_tasks.discard)


