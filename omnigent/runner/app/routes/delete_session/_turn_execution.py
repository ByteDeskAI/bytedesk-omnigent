    async def _run_turn_bg(
        msg_body: dict[str, Any],
        conv: str,
    ) -> None:
        """
        Run one session turn in the background.

        Resolves the agent spec, builds a ``TurnDispatch`` context
        with harness type / instructions / MCP hint, loads
        conversation history, assembles the harness body with tool
        schemas, and streams the turn via
        ``_stream_message_to_harness``.

        Called from both the initial ``post_session_events`` handler
        and from ``_check_and_start_next_turn`` for continuation
        turns (buffered mid-turn messages).

        :param msg_body: The forwarded message body from the server.
            Should include ``agent_id`` for harness resolution; when it
            doesn't (a message racing ahead of session assignment), the
            agent is resolved on demand from the server snapshot.
        :param conv: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """
        # This turn is consuming any previously-posted sub-agent wake notice.
        # Clear the debounce at turn start rather than turn end so a child
        # completion that lands while the parent is already reacting can post
        # the next wake. Otherwise a fast child can deliver into the inbox
        # during the stale debounce window and strand the result until the
        # human manually nudges the parent.
        _subagent_wake_pending.discard(conv)
        try:
            await _run_turn_bg_setup_and_stream(msg_body, conv)
        except _ContextWindowOverflow:
            # The streaming phase handles reactive compaction itself; this
            # guard only catches setup-phase failures (spec resolution,
            # spawn-env build, instruction/tool assembly). Re-raise so the
            # streaming path's own handler is never shadowed.
            raise
        except Exception as exc:
            # Any failure before the harness stream starts (e.g. a provider
            # with no resolvable model raising OmnigentError from
            # ``_build_spawn_env_from_spec``) must still end the turn: clear
            # ``_active_turns`` and publish a terminal ``failed`` status via
            # ``_on_proxy_stream_end``. Without this, the session stays pinned
            # to "running" forever and the REPL spins on "working" with no
            # output (the silent-hang failure mode).
            _logger.error(
                "turn setup failed for %s: %s",
                conv,
                exc,
                exc_info=True,
            )
            _on_proxy_stream_end(conv, error={"message": f"turn setup failed: {exc}"})


    async def _run_turn_bg_setup_and_stream(
        msg_body: dict[str, Any],
        conv: str,
    ) -> None:
        """
        Resolve the spec, build the dispatch context, and stream one turn.

        Split out of :func:`_run_turn_bg` so the setup phase (spec
        resolution, spawn-env build, instruction/tool assembly) is covered
        by the same terminal-status guard as the streaming phase. Any
        exception raised here propagates to ``_run_turn_bg``'s handler,
        which clears ``_active_turns`` and publishes a ``failed`` status so
        the client never hangs on a stale "running" turn.

        :param msg_body: The forwarded message body from the server.
        :param conv: Session/conversation identifier, e.g. ``"conv_abc123"``.
        """
        # In-place agent switch (POST /v1/sessions/{id}/switch-agent) rebinds
        # the session to a different agent mid-session. The server forwards the
        # NEW agent_id on the next turn; when it differs from the agent this
        # runner last served for the session, drop every spec-derived
        # per-session cache and tear down the old harness subprocess so the new
        # agent's spec, harness, tools, model, and (for a native target) the
        # freshly cleared external_session_id + carry-history label all take
        # effect below instead of stale values. The session-keyed spec cache is
        # otherwise never invalidated within a session's lifetime.
        _dispatched_agent_id = msg_body.get("agent_id")
        _prior_agent_id = _session_agent_ids.get(conv)
        if (
            _dispatched_agent_id
            and _prior_agent_id is not None
            and _prior_agent_id != _dispatched_agent_id
        ):
            _logger.info(
                "agent switch detected for %s: %s -> %s; resetting session caches",
                conv,
                _prior_agent_id,
                _dispatched_agent_id,
            )
            _session_spec_cache.pop(conv, None)
            _session_skills_cache.pop(conv, None)
            _session_tool_schemas.pop(conv, None)
            _compaction_contexts.pop(conv, None)
            # The AP snapshot carries external_session_id + labels, which the
            # switch just changed (cleared id, stamped carry-history); re-fetch.
            _session_snapshot_cache.pop(conv, None)
            if process_manager is not None:
                # Force a cold-start of the new harness: the per-conversation
                # subprocess bakes harness/model/auth/MCP env at spawn time.
                await process_manager.release(conv)
        if _dispatched_agent_id:
            _session_agent_ids[conv] = _dispatched_agent_id

        cached_spec_entry = _session_spec_cache.get(conv)
        cached_spec = _unwrap_resolved_spec(cached_spec_entry)
        cached_spec_workdir = _resolved_spec_workdir(cached_spec_entry)
        if cached_spec is None and spec_resolver is not None:
            _aid = msg_body.get("agent_id")
            if _aid:
                try:
                    resolved = await spec_resolver(_aid, conv)
                    if isinstance(resolved, ResolvedSpec):
                        cached_spec = _unwrap_resolved_spec(resolved)
                        cached_spec_workdir = _resolved_spec_workdir(resolved)
                        _session_spec_cache[conv] = resolved
                    elif resolved is not None:
                        cached_spec = resolved
                        _session_spec_cache[conv] = resolved
                except (httpx.HTTPError, RuntimeError):
                    _logger.warning(
                        "Spec resolution failed for %s",
                        conv,
                        exc_info=True,
                    )
            else:
                # The forwarded message can race ahead of the session
                # assignment (POST /v1/sessions), arriving with no
                # agent_id before the spec cache is populated. Resolve
                # the agent from the authoritative server snapshot
                # (GET /v1/sessions/{conv}) instead of the turn being
                # silently dropped (first-message race).
                try:
                    cached_spec = await _resolve_session_agent_spec(conv)
                    # _resolve_session_agent_spec returns the unwrapped
                    # spec but caches the ResolvedSpec entry — re-read it
                    # to recover the workdir the unwrap drops.
                    cached_spec_workdir = _resolved_spec_workdir(_session_spec_cache.get(conv))
                except (OmnigentError, httpx.HTTPError, RuntimeError):
                    _logger.warning(
                        "On-demand agent resolution failed for %s",
                        conv,
                        exc_info=True,
                    )

        # Sub-agent spec resolution: if this session is a child,
        # find the sub-agent's spec in the parent's spec tree
        # instead of using the root spec directly. This ensures
        # the child gets the sub-agent's prompt/tools, not the
        # parent's (which would cause infinite recursion via
        # sys_session_send).
        #
        # Recover the name from the server snapshot when the in-memory map
        # was lost (runner restart / tunnel reconnect): without this, a
        # continuation turn for a claude-native sub-agent resolves the
        # parent's claude-sdk harness, the process manager respawns, and the
        # child's native terminal is torn down ("Bridge closed: terminal
        # resource not found"). The snapshot carries sub_agent_name; this
        # is the primary turn path (the harness baked into TurnDispatch
        # below comes from the swapped spec, so it must be correct here).
        _sa_name = await _recover_sub_agent_name(conv)
        if _sa_name and cached_spec is not None:
            from omnigent.runtime.workflow import _find_spec_by_name

            sub_spec = _find_spec_by_name(cached_spec, _sa_name)
            if sub_spec is not None:
                cached_spec = sub_spec
                _session_spec_cache[conv] = (
                    ResolvedSpec(spec=cached_spec, workdir=cached_spec_workdir)
                    if cached_spec_workdir is not None
                    else cached_spec
                )

        cached_spec = _spec_with_workdir_paths(cached_spec, cached_spec_workdir)
        if cached_spec is not None:
            _session_spec_cache[conv] = (
                ResolvedSpec(spec=cached_spec, workdir=cached_spec_workdir)
                if cached_spec_workdir is not None
                else cached_spec
            )

        harness_name: str | None = None
        spawn_env: dict[str, str] | None = None
        instructions: str | None = None
        if cached_spec is not None:
            # The per-session harness override (validated at session
            # create, forwarded by the Omnigent server in the message
            # body) replaces the spec's declared brain harness.
            h = (
                msg_body.get("harness_override")
                or cached_spec.executor.config.get("harness")
                or cached_spec.executor.type
            )
            harness_name = canonicalize_harness(h) or h
            spawn_env = _build_spawn_env_from_spec(
                cached_spec,
                harness_name,
                workdir=cached_spec_workdir,
                # Apply the per-session /model override so it actually
                # changes the model on the SDK harnesses (not just the
                # readout). Forwarded by the Omnigent server in the message body.
                model_override=msg_body.get("model_override"),
            )
            from omnigent.kernel.extensions import extension_instruction_fragments
            from omnigent.runtime.prompt import build_instructions

            agent_id = msg_body.get("agent_id")
            forwarded_fragments_raw = msg_body.get("instruction_fragments")
            instruction_fragments: list[str] = []
            if isinstance(forwarded_fragments_raw, list):
                instruction_fragments.extend(
                    fragment
                    for fragment in forwarded_fragments_raw
                    if isinstance(fragment, str) and fragment
                )
            instruction_fragments.extend(
                extension_instruction_fragments(
                    agent_id=agent_id if isinstance(agent_id, str) else None,
                    spec=cached_spec,
                )
            )
            instructions = build_instructions(
                cached_spec,
                None,
                [],
                instruction_fragments,
            )

        ctx = TurnDispatch(
            agent_id=msg_body.get("agent_id"),
            harness=harness_name,
            spawn_env=spawn_env,
            has_mcp_servers=(
                (cached_spec is not None and bool(cached_spec.mcp_servers))
                or msg_body.get("has_mcp_servers") is True
            ),
            instructions=instructions,
        )

        if conv not in _session_histories:
            _session_histories[conv] = await _load_history_as_input(conv)

        if conv not in _compaction_contexts:
            from omnigent.llms.context_window import get_model_context_window

            _model: str | None = None
            _compaction_cfg = None
            if cached_spec is not None:
                from omnigent.runtime.workflow import _resolve_spec_model

                _model = _resolve_spec_model(cached_spec)
                _compaction_cfg = cached_spec.compaction
            if not _model:
                _model = msg_body.get("model") or "unknown"
            _ctx_window = get_model_context_window(_model)
            if _ctx_window is not None:
                _compaction_contexts[conv] = {
                    "context_window": _ctx_window,
                    "model": _model,
                    "config": _compaction_cfg,
                }

        # Proactive compaction: if the history exceeds the token
        # budget, compact before sending to the harness.
        _cc = _compaction_contexts.get(conv)
        if _cc and _session_histories[conv]:
            await _proactive_compact_if_needed(
                conv,
                _cc,
                cached_spec,
            )

        harness_body: dict[str, Any] = {
            "type": "message",
            "role": "user",
            "model": msg_body.get("model", ""),
        }
        if _session_histories[conv]:
            harness_body["content"] = _session_histories[conv]
        else:
            harness_body["content"] = msg_body.get(
                "content",
                [],
            )
        _content = harness_body.get("content", [])
        _content_summary = []
        for _ci in _content:
            if isinstance(_ci, dict):
                _ct = _ci.get("type", "?")
                if _ct == "message":
                    _blocks = _ci.get("content", [])
                    _block_types = [b.get("type") for b in _blocks if isinstance(b, dict)]
                    _content_summary.append(f"msg({_ci.get('role', '?')}, blocks={_block_types})")
                else:
                    _content_summary.append(_ct)
        _logger.info(
            "_run_turn_bg: conv=%s history_msgs=%d content_summary=%s",
            conv,
            len(_content),
            _content_summary[:20],
        )

        # Cost advisor (dark by default): judge this turn's difficulty,
        # persist the cost_control.plan verdict label, and — optimize mode
        # on a claude-sdk brain with no user pin — run the brain on the
        # verdict model this turn and inject the one-line note. No-op
        # unless executor.config.cost_optimize is set.
        _advisor_result = await _run_turn_advisor(msg_body, conv, cached_spec)
        # harness_body is rebuilt without the inbound model_override, so the
        # user pin must be passed explicitly or the sticky stamp beats it.
        _apply_advisor_for_turn(
            harness_body, conv, _advisor_result, msg_body.get("model_override")
        )

        if instructions:
            harness_body["instructions"] = instructions

        if conv not in _session_tool_schemas:
            all_tools: list[dict[str, Any]] = []
            if cached_spec is not None:
                try:
                    from omnigent.tools.manager import (
                        ToolManager,
                    )

                    _tmgr = ToolManager(
                        cached_spec,
                        workdir=cached_spec_workdir or runner_workspace,
                    )
                    all_tools.extend(_tmgr.get_tool_schemas())
                except (
                    ImportError,
                    ValueError,
                    RuntimeError,
                ):
                    _logger.warning(
                        "ToolManager schema build failed for %s",
                        conv,
                        exc_info=True,
                    )
            _session_mcp: Any = ProxyMcpManager(conv, server_client)
            if cached_spec and cached_spec.mcp_servers and _session_mcp:
                try:
                    mcp_result = await _session_mcp.schemas_for(
                        cached_spec,
                    )
                    all_tools.extend(mcp_result.schemas)
                except (
                    httpx.HTTPError,
                    RuntimeError,
                    ValueError,
                ):
                    _logger.warning(
                        "MCP schema resolution failed for %s",
                        conv,
                        exc_info=True,
                    )
            _session_tool_schemas[conv] = all_tools

        # Spec builtin + MCP schemas are cached per conversation, but the
        # caller's client-side tools arrive per event on ``msg_body["tools"]``
        # — merge them in so non-native harnesses see ``request.tools`` and
        # the model can emit (and tunnel) client-side tool calls.
        _spec_tools = _session_tool_schemas.get(conv) or []
        _client_tools = msg_body.get("tools") or []
        merged_tools = _merge_request_client_tools(_spec_tools, _client_tools)
        if merged_tools:
            harness_body["tools"] = merged_tools
        # Record which tools are client-side (request-supplied and not part
        # of the spec's builtin/MCP/local surface) so the proxy_stream relays
        # their action_required events upstream to tunnel — rather than
        # dispatching them locally, which would error "not in local dispatch
        # table". A request tool that collides with a spec tool name is NOT
        # client-side: the builtin wins (see _merge_request_client_tools).
        _spec_names = {
            name
            for t in _spec_tools
            if isinstance(t, dict) and (name := _schema_tool_name(t)) is not None
        }
        ctx.client_side_tool_names = frozenset(
            name
            for t in _client_tools
            if isinstance(t, dict)
            and (name := _schema_tool_name(t)) is not None
            and name not in _spec_names
        )

        # Fallback for native sessions whose terminal was launched
        # outside the runner terminal route (e.g. tests, UI-launched
        # terminals): make sure the comment-tool relay is running before the
        # user message is injected. The normal ``omnigent claude`` /
        # ``omnigent codex`` path already started it at terminal launch, in
        # which case this is a no-op. ``await_notify=False``: a UI-launched
        # terminal is never pre-warmed, so on its first turn Claude Code's MCP
        # bridge has not published ``server.json`` yet and awaiting the
        # tools/list_changed delivery would stall the turn ~15s on
        # ``post_tools_changed``'s readiness poll. ``tool_relay.json`` is
        # already on disk synchronously, so fire the notification in the
        # background instead — the relay tools land a beat later, which is
        # harmless on the first turn (nobody reads comments before sending).
        if harness_name == "claude-native":
            await _ensure_comment_relay_started(conv, await_notify=False)
        elif harness_name == "codex-native":
            from omnigent.codex_native_bridge import (
                CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
                write_mcp_bridge_config,
            )
            from omnigent.codex_native_bridge import (
                bridge_dir_for_bridge_id as codex_bridge_dir_for_id,
            )

            codex_labels = await _session_labels_for_runner_spawn(
                server_client=server_client,
                session_id=conv,
            )
            codex_bid = codex_labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY)
            codex_bdir = codex_bridge_dir_for_id(codex_bid or conv)
            write_mcp_bridge_config(codex_bdir)
            # Fallback for sessions not started via _auto_create_codex_terminal
            # (which already started the relay). await_notify=False: codex's MCP
            # bridge is lazy, so awaiting would stall the turn (see the
            # _ensure_comment_relay_started docstring).
            await _ensure_comment_relay_started(
                conv, explicit_bridge_dir=codex_bdir, await_notify=False
            )

        try:
            response = await _stream_message_to_harness(
                harness_body,
                conv,
                dispatch=ctx,
            )
            if isinstance(response, StreamingResponse):
                await _drain_streaming_response(response, conv)
            else:
                err_detail = "harness returned error response"
                if hasattr(response, "body"):
                    with contextlib.suppress(
                        UnicodeDecodeError,
                        AttributeError,
                    ):
                        err_detail = response.body.decode(
                            "utf-8",
                        )[:200]
                _logger.error(
                    "turn bg error for %s: %s",
                    conv,
                    err_detail,
                )
                _on_proxy_stream_end(
                    conv,
                    error={"message": err_detail},
                )
        except _ContextWindowOverflow as overflow:
            _logger.info(
                "Reactive compaction for session=%s: %d > %d",
                conv,
                overflow.actual_tokens,
                overflow.max_tokens,
            )
            _cc = _compaction_contexts.get(conv)
            if _cc is None:
                _cc = {
                    "context_window": overflow.max_tokens,
                    "model": msg_body.get("model", "unknown"),
                    "config": (cached_spec.compaction if cached_spec else None),
                }
                _compaction_contexts[conv] = _cc
            else:
                _cc["context_window"] = overflow.max_tokens

            await _proactive_compact_if_needed(conv, _cc, cached_spec)

            # The compacted history replaces the body's content wholesale,
            # which would silently drop the per-turn advisor note — re-merge
            # it so the retried turn still announces the applied model
            # (_merge_advisor_note is copy-on-write: the cached history list
            # must not carry the note). The advisor's
            # harness_body["model_override"] is a separate key and survives
            # the content rebuild untouched.
            if _advisor_result is not None and _advisor_result.note_item is not None:
                harness_body["content"] = _merge_advisor_note(
                    _session_histories[conv],
                    _advisor_result.note_item,
                )
            else:
                harness_body["content"] = _session_histories[conv]
            try:
                retry_resp = await _stream_message_to_harness(
                    harness_body,
                    conv,
                    dispatch=ctx,
                )
                if isinstance(retry_resp, StreamingResponse):
                    await _drain_streaming_response(retry_resp, conv)
                else:
                    _on_proxy_stream_end(
                        conv,
                        error={
                            "message": ("Context window exceeded after compaction"),
                        },
                    )
            except _ContextWindowOverflow:
                _logger.error(
                    "Context window overflow persists after compaction "
                    "for session=%s; ending turn",
                    conv,
                )
                _on_proxy_stream_end(
                    conv,
                    error={
                        "message": ("Context window exceeded after compaction"),
                    },
                )
            except Exception:
                _logger.exception(
                    "Unexpected error on post-compaction retry for session=%s",
                    conv,
                )
                _on_proxy_stream_end(
                    conv,
                    error={
                        "message": ("Unexpected error on post-compaction retry"),
                    },
                )


    async def _drain_streaming_response(
        response: StreamingResponse,
        session_id: str,
    ) -> None:
        """
        Consume a background turn's ``StreamingResponse`` to completion.

        The ``proxy_stream`` generator publishes events to
        ``session_stream`` as it runs; the bytes themselves are
        discarded since there is no HTTP client to receive them.
        Turn-end bookkeeping is handled by ``proxy_stream`` calling
        ``_on_proxy_stream_end`` at its completion points.

        :param response: The ``StreamingResponse`` wrapping
            ``proxy_stream()``.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """
        try:
            async for _chunk in response.body_iterator:
                pass
        except asyncio.CancelledError:
            # Publish terminal status so the client doesn't sit on stale "running".
            _active_turns.pop(session_id, None)
            _publish_turn_status(session_id, "idle")
            raise
        except _ContextWindowOverflow:
            raise
        except (httpx.HTTPError, RuntimeError, StopAsyncIteration) as exc:
            _logger.error(
                "drain failed for %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
            _on_proxy_stream_end(
                session_id,
                error={
                    "message": f"background turn drain failed: {exc}",
                },
            )


    async def _stream_message_to_harness(
        body: dict[str, Any],
        conv_id: str,
        dispatch: TurnDispatch | None = None,
    ) -> Any:
        """Stream one session message through the runner-owned harness.

        :param body: The harness message body — only fields the
            harness needs (type, role, content, model). No
            runner-only metadata.
        :param conv_id: Conversation/session identifier.
        :param dispatch: Runner dispatch context. When provided,
            used for harness resolution, MCP injection, and
            system prompt. When ``None`` (legacy callers), these
            are read from ``body`` for backward compatibility.
        """
        # Read dispatch context — prefer TurnDispatch, fall back
        # to body fields for legacy callers.
        harness_name = dispatch.harness if dispatch else body.get("harness")
        spawn_env = dispatch.spawn_env if dispatch else body.get("spawn_env")
        if not harness_name:
            _agent_id = dispatch.agent_id if dispatch else body.get("agent_id")
            # Recover the sub-agent name (server snapshot if the in-memory
            # map was lost on reconnect) so a child session resolves its OWN
            # harness, not the parent's. Without this a continuation turn for
            # a claude-native sub-agent resolves the parent claude-sdk harness
            # and respawns, killing the native terminal ("Bridge closed").
            _sub_agent_name = await _recover_sub_agent_name(conv_id)
            try:
                harness_name, spawn_env = await _resolve_harness_config(
                    agent_id=_agent_id,
                    spec_resolver=spec_resolver,
                    session_id=conv_id,
                    model_override=body.get("model_override"),
                    harness_override=body.get("harness_override"),
                    sub_agent_name=_sub_agent_name,
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "spec_resolver_failed",
                        "detail": _client_safe_error_detail(exc, context="spec resolve"),
                    },
                )
        if harness_name == "claude-native" and spawn_env is None:
            from omnigent.claude_native_bridge import build_claude_native_spawn_env

            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client,
                session_id=conv_id,
            )
            spawn_env = build_claude_native_spawn_env(conv_id, bridge_id=bridge_id)
        if harness_name == "codex-native" and spawn_env is None:
            from omnigent.codex_native_bridge import (
                CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
                build_codex_native_spawn_env,
            )

            labels = await _session_labels_for_runner_spawn(
                server_client=server_client,
                session_id=conv_id,
            )
            bridge_id = labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY)
            spawn_env = build_codex_native_spawn_env(conv_id, bridge_id=bridge_id)
        if harness_name == "pi-native" and spawn_env is None:
            from omnigent.pi_native_bridge import build_pi_native_spawn_env

            spawn_env = build_pi_native_spawn_env(conv_id)

        agent_version = dispatch.agent_version if dispatch else body.get("agent_version")
        if agent_version is not None and conv_id in _version_cache:
            if agent_version > _version_cache[conv_id]:
                await process_manager.release(conv_id)
        if agent_version is not None:
            _version_cache[conv_id] = agent_version

        try:
            client = await process_manager.get_client(conv_id, harness_name, env=spawn_env)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "harness_spawn_failed",
                    "detail": _client_safe_error_detail(exc, context="harness spawn"),
                },
            )

        _turn_agent_id = dispatch.agent_id if dispatch else body.get("agent_id")
        _has_mcp_hint = dispatch.has_mcp_servers if dispatch else body.get("has_mcp_servers")
        _turn_spec: Any = None
        _turn_spec_resolved = False
        _mcp_schemas: list[dict[str, Any]] = []
        _mcp_tool_names: set[str] = set()
        _eager_spec_error: tuple[str, str] | None = None
        if _has_mcp_hint is True and _turn_agent_id:
            # Check both spec caches: agent-keyed (MCP path) and
            # session-keyed (session creation path).
            _turn_spec_entry = _spec_cache.get(_turn_agent_id)
            _turn_spec = _unwrap_resolved_spec(_turn_spec_entry)
            if _turn_spec is None:
                _session_entry = _session_spec_cache.get(conv_id)
                _turn_spec = _unwrap_resolved_spec(_session_entry)
            if _turn_spec is None and spec_resolver is not None:
                try:
                    _resolved_turn_spec = await spec_resolver(_turn_agent_id, conv_id)
                    _turn_spec = _unwrap_resolved_spec(_resolved_turn_spec)
                except (httpx.HTTPError, RuntimeError) as exc:
                    # Keep the exception class (a safe, generic label) for the
                    # client; log the full cause for operators. The raw message
                    # can embed internal hosts/paths, so it stays out of the
                    # streamed failure event.
                    _logger.warning(
                        "eager turn spec resolution failed for %s: %s",
                        conv_id,
                        exc,
                        exc_info=True,
                    )
                    _eager_spec_error = (
                        type(exc).__name__,
                        "Failed to resolve the agent spec for this turn.",
                    )
                else:
                    if _turn_spec is not None:
                        _spec_cache[_turn_agent_id] = _resolved_turn_spec
            _turn_spec_resolved = True
            _turn_mcp: Any = ProxyMcpManager(conv_id, server_client)
            if _eager_spec_error is None and _turn_spec is not None:
                try:
                    _mcp = await _turn_mcp.schemas_for(_turn_spec)
                    _mcp_schemas = _mcp.schemas
                    _mcp_tool_names = _mcp.tool_names
                    for _srv, _err in _mcp.failures.items():
                        _logger.warning("runner MCP %r unavailable for this turn: %s", _srv, _err)
                except Exception:
                    _logger.exception("runner mcp_manager.schemas_for failed")

        async def _resolve_turn_spec_lazy() -> tuple[Any, tuple[str, str] | None]:
            """Resolve spec on demand for non-eager (non-MCP) turns.

            Returns ``(spec, None)`` on success or ``(None, (type, msg))``
            on resolver failure. Caller decides how to surface the error
            (typically ``_response_failed_event`` from inside the SSE
            generator).
            """
            nonlocal _turn_spec, _turn_spec_resolved
            if _turn_spec_resolved:
                return _turn_spec, None
            _turn_spec_resolved = True
            # Session-level cache has the sub-agent's resolved spec
            # (set by _run_turn_bg) for child sessions. Check it
            # first so sub-agent turns dispatch tools against the
            # sub-spec, not the root spec.
            session_cached = _session_spec_cache.get(conv_id)
            if session_cached is not None:
                _turn_spec = _unwrap_resolved_spec(session_cached)
                return _turn_spec, None
            if not _turn_agent_id or spec_resolver is None:
                return None, None
            cached = _spec_cache.get(_turn_agent_id)
            if cached is not None:
                _turn_spec = _unwrap_resolved_spec(cached)
                return _turn_spec, None
            try:
                resolved = await spec_resolver(_turn_agent_id, conv_id)
            except (httpx.HTTPError, RuntimeError) as exc:
                _logger.warning(
                    "lazy turn spec resolution failed for %s: %s",
                    conv_id,
                    exc,
                    exc_info=True,
                )
                return None, (
                    type(exc).__name__,
                    "Failed to resolve the agent spec for this turn.",
                )
            if resolved is not None:
                _spec_cache[_turn_agent_id] = resolved
                _turn_spec = _unwrap_resolved_spec(resolved)
            return _turn_spec, None

        async def proxy_stream():
            # If eager spec resolution failed (MCP path), emit the
            # SSE failure now — the harness was never POSTed so no
            # response.created was produced.
            import asyncio as _asyncio
            import json as _json

            from omnigent.runner.tool_dispatch import (
                dispatch_tool_locally,
                get_arguments,
                get_call_id,
                get_tool_name,
                is_action_required,
                should_dispatch_locally,
            )

            if _eager_spec_error is not None:
                _err_type, _err_msg = _eager_spec_error
                _fail = {
                    "type": "response.failed",
                    "error": {
                        "message": _err_msg,
                        "type": _err_type,
                    },
                }
                _publish_event(conv_id, _fail)
                _on_proxy_stream_end(
                    conv_id,
                    error={"message": _err_msg, "type": _err_type},
                )
                yield _response_failed_event({"message": _err_msg, "type": _err_type})
                return

            event_body = _wrap_as_message_event(body)
            # Inject the spec's builtin tool schemas (sys_agent_list,
            # sys_session_create, …). Unlike the fire-and-forget path
            # (_run_turn_bg, which assembles builtins + MCP), the streaming
            # path otherwise injects ONLY MCP schemas, so a streaming agent
            # (e.g. Maya on the Office SSE bridge) never sees its
            # orchestration builtins and the model gets "No such tool
            # available: mcp__omnigent__sys_agent_list" (BDP-2204). Resolve
            # the turn spec via the idempotent lazy resolver (already cached
            # for the eager MCP path) so this also covers builtin-only /
            # non-MCP streaming turns.
            _builtin_spec, _builtin_spec_err = await _resolve_turn_spec_lazy()
            if _builtin_spec_err is None:
                _inject_mcp_schemas(
                    event_body,
                    _spec_builtin_tool_schemas(_builtin_spec, runner_workspace),
                )
            _inject_mcp_schemas(event_body, _mcp_schemas)
            try:
                async with client.stream(
                    "POST",
                    f"/v1/sessions/{conv_id}/events",
                    json=event_body,
                    timeout=None,
                ) as harness_resp:
                    if harness_resp.status_code != 200:
                        _fail_status = {
                            "type": "response.failed",
                            "error": {
                                "status": harness_resp.status_code,
                            },
                        }
                        _publish_event(
                            conv_id,
                            _fail_status,
                        )
                        _on_proxy_stream_end(
                            conv_id,
                            error={"status": harness_resp.status_code},
                        )
                        yield _response_failed_event({"status": harness_resp.status_code})
                        return

                    # Relay every SSE frame upstream. For
                    # action_required tool calls that match the
                    # local dispatch table, the runner executes
                    # the tool and PATCHes the harness — the
                    # harness then emits a function_call_output
                    # that flows through here for the executor's
                    # pairing buffer. The action_required event
                    # itself is STILL relayed so the executor
                    # emits ToolCallInProgress for REPL rendering
                    # (the executor skips its own dispatch when
                    # handles_tool_dispatch is set on the process
                    # manager).
                    _response_id: str | None = None
                    _omnigent_task_id: str | None = body.get("task_id")
                    _buffer = ""
                    _dispatch_tasks: list[_asyncio.Task[str]] = []
                    _text_acc: list[str] = []
                    # Last failure seen in the harness stream. Threaded into
                    # _on_proxy_stream_end so a turn that ends after a
                    # response.failed publishes session.status "failed", not
                    # "idle". Critical for codex-native: "idle" is suppressed
                    # there (the app-server forwarder owns it), so without
                    # this the client's working indicator never clears.
                    _stream_failed_error: dict[str, Any] | None = None
                    async for chunk in harness_resp.aiter_text():
                        _buffer += chunk
                        while "\n\n" in _buffer:
                            frame, _, _buffer = _buffer.partition("\n\n")
                            raw_sse_bytes = (frame + "\n\n").encode("utf-8")

                            data_line = next(
                                (line for line in frame.splitlines() if line.startswith("data:")),
                                None,
                            )
                            if data_line is not None:
                                try:
                                    event = _json.loads(data_line[5:].strip())
                                except _json.JSONDecodeError:
                                    event = None
                            else:
                                event = None

                            if event is not None:
                                if event.get("type") == "response.created":
                                    resp_obj = event.get("response") or {}
                                    _response_id = resp_obj.get("id")
                                    if _response_id and conv_id:
                                        _resp_to_conv[_response_id] = conv_id

                                # Defer publish for action_required
                                # events that the runner dispatches
                                # locally — publishing before dispatch
                                # would leak the action_required to the
                                # client before the runner can handle it.
                                _defer_publish = False

                                # Detect context-window overflow from
                                # the harness. Raises so _run_turn_bg
                                # can run reactive compaction and retry.
                                _overflow = _is_context_overflow_error(event)
                                if _overflow is not None:
                                    raise _ContextWindowOverflow(*_overflow)

                                # Build in-memory history from
                                # SSE events: text deltas, tool
                                # calls, and tool results.
                                _evt_type = event.get("type")
                                if _evt_type == "injection.consumed":
                                    # Runner-internal exactly-once marker
                                    # (RUNNER_MESSAGE_INGEST.md Part B): the
                                    # harness consumed this mid-turn
                                    # injection into the live turn. Drop the
                                    # buffered copy so it does not also drive
                                    # a continuation turn, and record it in
                                    # history once (the live turn — not a
                                    # continuation — is where it reached the
                                    # LLM). Never published to the client or
                                    # relayed upstream.
                                    _inj_id = event.get("injection_id")
                                    _buf = _session_message_buffers.get(conv_id)
                                    if _inj_id is not None and _buf:
                                        _consumed = [
                                            _m for _m in _buf if _m.get("injection_id") == _inj_id
                                        ]
                                        _remaining = [
                                            _m for _m in _buf if _m.get("injection_id") != _inj_id
                                        ]
                                        _session_message_buffers[conv_id] = _remaining
                                        for _m in _consumed:
                                            _session_histories.setdefault(conv_id, []).append(
                                                {
                                                    "type": "message",
                                                    "role": _m.get("role", "user"),
                                                    "content": _m.get("content", []),
                                                }
                                            )
                                    continue
                                if _evt_type == "response.output_text.delta":
                                    delta = event.get("delta")
                                    if delta is not None:
                                        _text_acc.append(delta)
                                elif _evt_type == "response.completed":
                                    # A completion supersedes any earlier
                                    # in-stream failure — the turn ended
                                    # successfully, so the stream end must
                                    # publish "idle", not "failed".
                                    _stream_failed_error = None
                                    if _text_acc:
                                        _session_histories.setdefault(conv_id, []).append(
                                            {
                                                "type": "message",
                                                "role": "assistant",
                                                "content": [
                                                    {
                                                        "type": "output_text",
                                                        "text": "".join(_text_acc),
                                                    }
                                                ],
                                            }
                                        )
                                        _text_acc.clear()
                                    # Capture provider-reported usage for
                                    # compaction estimation. More accurate
                                    # than tiktoken for harness executors
                                    # whose internal session is larger than
                                    # what the runner persists.
                                    _resp = event.get("response")
                                    if isinstance(_resp, dict):
                                        _usage = _resp.get("usage")
                                        if isinstance(_usage, dict):
                                            _ctx = _usage.get("context_tokens") or _usage.get(
                                                "total_tokens"
                                            )
                                            if isinstance(_ctx, int) and _ctx > 0:
                                                _cc_ref = _compaction_contexts.get(conv_id)
                                                if _cc_ref is not None:
                                                    _cc_ref["provider_tokens"] = _ctx
                                elif _evt_type == "response.failed":
                                    # Remember the failure so the stream-end
                                    # bookkeeping publishes a terminal
                                    # "failed" status. The frame itself is
                                    # still relayed/published below — this
                                    # only captures the error payload.
                                    _err = event.get("error") or (event.get("response") or {}).get(
                                        "error"
                                    )
                                    _stream_failed_error = (
                                        _err
                                        if isinstance(_err, dict)
                                        # Scaffolds always attach an error
                                        # dict; this fallback only covers a
                                        # malformed frame so the terminal
                                        # edge still carries a message.
                                        else {"message": "harness turn failed"}
                                    )
                                elif _evt_type == "response.output_item.done":
                                    _item = event.get("item")
                                    if isinstance(_item, dict):
                                        _it = _item.get("type")
                                        if _it == "function_call":
                                            _session_histories.setdefault(conv_id, []).append(
                                                {
                                                    "type": "function_call",
                                                    "call_id": _item["call_id"],
                                                    "name": _item["name"],
                                                    "arguments": _item["arguments"],
                                                }
                                            )
                                        elif _it == "function_call_output":
                                            _session_histories.setdefault(conv_id, []).append(
                                                {
                                                    "type": "function_call_output",
                                                    "call_id": _item["call_id"],
                                                    "output": _item["output"],
                                                }
                                            )

                                if is_action_required(event):
                                    tool_name = get_tool_name(event)
                                    is_mcp = tool_name in _mcp_tool_names
                                    _spec_for_dispatch_hint = _unwrap_resolved_spec(
                                        _session_spec_cache.get(conv_id)
                                    )
                                    _is_spec_local = any(
                                        getattr(info, "name", None) == tool_name
                                        and getattr(info, "language", None)
                                        in ("python", "omnigent-python-callable")
                                        for info in getattr(
                                            _spec_for_dispatch_hint, "local_tools", []
                                        )
                                    )
                                    _should_dispatch = _should_dispatch_tool_locally(
                                        tool_name,
                                        dispatch=dispatch,
                                        is_mcp=is_mcp,
                                        is_runner_builtin=should_dispatch_locally(tool_name),
                                        is_spec_local=_is_spec_local,
                                    )
                                    if _should_dispatch and _response_id:
                                        _defer_publish = True
                                        # Lazy spec resolution for non-eager
                                        # (non-MCP) paths. spec_resolver
                                        # failures surface as response.failed
                                        # SSE (see the response.failed contract).
                                        (
                                            _spec_for_dispatch,
                                            _lazy_err,
                                        ) = await _resolve_turn_spec_lazy()
                                        if _lazy_err is not None:
                                            _err_type, _err_msg = _lazy_err
                                            yield _response_failed_event(
                                                {"message": _err_msg, "type": _err_type}
                                            )
                                            return
                                        # All tool calls go through AP:/mcp
                                        # (ProxyMcpManager in Omnigent mode), which
                                        # enforces TOOL_CALL + TOOL_RESULT
                                        # policies server-side before forwarding
                                        # to the runner's /mcp/execute.
                                        event[_RUNNER_DISPATCHED_FIELD] = True
                                        raw_sse_bytes = _encode_sse_event(event)
                                        _agent_id_for_dispatch = body.get("agent_id")
                                        _dispatch_mcp: Any = ProxyMcpManager(
                                            conv_id,
                                            server_client,
                                            publish_event=_publish_event,
                                        )
                                        _dispatch_tasks.append(
                                            _asyncio.create_task(
                                                dispatch_tool_locally(
                                                    tool_name=tool_name,
                                                    call_id=get_call_id(event),
                                                    arguments=get_arguments(event),
                                                    response_id=_response_id,
                                                    harness_client=client,
                                                    server_client=server_client,
                                                    terminal_registry=terminal_registry,
                                                    resource_registry=resource_registry,
                                                    agent_spec=_spec_for_dispatch,
                                                    conversation_id=conv_id,
                                                    task_id=_omnigent_task_id or _response_id,
                                                    agent_id=_agent_id_for_dispatch,
                                                    agent_name=body.get("model"),
                                                    runner_workspace=runner_workspace,
                                                    mcp_manager=_dispatch_mcp,
                                                    session_inbox=_session_inboxes.get(conv_id),
                                                    session_async_tasks=_session_async_tasks.get(
                                                        conv_id
                                                    ),
                                                    publish_event=_publish_event,
                                                    filesystem_registry=filesystem_registry,
                                                )
                                            )
                                        )

                                # ── Policy evaluation round-trip ──
                                # The harness emits this when the inner
                                # executor is about to make (or just made)
                                # an LLM call and needs an LLM_REQUEST /
                                # LLM_RESPONSE policy verdict. The runner
                                # proxies the request to the Omnigent server's
                                # evaluate endpoint and posts the verdict
                                # back to the harness as a policy_verdict
                                # inbound event. The SSE frame is consumed
                                # here — never relayed to clients.
                                if _evt_type == "policy_evaluation.requested":
                                    _eval_id = event.get("evaluation_id", "")
                                    _eval_phase = event.get("phase", "")
                                    _eval_data = event.get("data") or {}
                                    _dispatch_tasks.append(
                                        _asyncio.create_task(
                                            _evaluate_policy_via_omnigent(
                                                server_client=server_client,
                                                harness_client=client,
                                                conversation_id=conv_id,
                                                evaluation_id=_eval_id,
                                                phase=_eval_phase,
                                                data=_eval_data,
                                            )
                                        )
                                    )
                                    # Don't relay or publish — runner-internal.
                                    continue

                            # Publish to session stream if not deferred
                            # by the dispatch path above. Suppress
                            # response.created — the sessions path
                            # does not use response_id.
                            if not _defer_publish and event.get("type") != "response.created":
                                _publish_event(conv_id, event)
                            # In sessions-native mode (dispatch is set),
                            # don't relay runner-dispatched action_required
                            # events — the client would try to handle them
                            # as client-side tools. In legacy mode
                            # (dispatch is None), the server-side executor
                            # needs to see the marker to skip its own
                            # dispatch.
                            if dispatch is not None and event.get(_RUNNER_DISPATCHED_FIELD):
                                pass
                            else:
                                yield raw_sse_bytes

                    if _dispatch_tasks:
                        await _asyncio.gather(*_dispatch_tasks, return_exceptions=True)

                    _on_proxy_stream_end(conv_id, error=_stream_failed_error)

            except (httpx.HTTPError, RuntimeError) as exc:
                # RuntimeError covers httpx.StreamClosed which
                # is NOT an HTTPError subclass — raised when the
                # harness subprocess dies mid-stream. Surface the
                # proxy-stream break as the same retryable code the
                # direct harness client uses for transport drops so
                # the AP-side L2 retry classifier can respawn the
                # harness and retry the turn.
                #
                # The retry classifier keys on ``code``/``type`` (not the
                # human message), so the message is a fixed, client-safe
                # string; the raw cause (which can embed the harness socket
                # path/host) is logged for operators only.
                _logger.warning(
                    "proxy stream connection error for %s: %s",
                    conv_id,
                    exc,
                    exc_info=True,
                )
                _error = {
                    "code": "connection_error",
                    "message": "Harness stream connection error.",
                    "type": type(exc).__name__,
                }
                _http_fail = {
                    "type": "response.failed",
                    "response": {"status": "failed", "error": _error},
                    "error": _error,
                }
                _publish_event(conv_id, _http_fail)
                _on_proxy_stream_end(conv_id, error=_error)
                yield _response_failed_event(_error)

        return StreamingResponse(
            proxy_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


