    @app.post("/v1/sessions")
    async def create_session(request: Request) -> JSONResponse:
        """
        Assign a session to this runner.

        The server calls this after creating the conversation in
        the conversation store. The runner eagerly spawns a harness
        subprocess and caches the agent spec so the session is
        ready to accept events immediately.

        Per ``designs/SESSION_REARCHITECTURE.md`` §4 step 3.

        :param request: JSON body with ``session_id`` and
            ``agent_id``.
        :returns: :class:`SessionResponse`-shaped JSON (201) on
            success; 400 for missing fields; 501 in scaffold mode.
        """
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": ("Runner POST /v1/sessions needs a HarnessProcessManager."),
                },
            )
        body = await request.json()
        session_id = body.get("session_id")
        agent_id = body.get("agent_id")
        if not session_id or not agent_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": ("'session_id' and 'agent_id' required."),
                },
            )

        # Resolve the spec once — derive harness config from it and
        # cache it for resource endpoints (filesystem, terminals)
        # that may fire before the first turn dispatches.
        spec = None
        if spec_resolver is not None:
            try:
                spec = await spec_resolver(agent_id, session_id)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "spec_resolver_failed",
                        "detail": _client_safe_error_detail(exc, context="spec resolve"),
                    },
                )
        if spec is not None:
            spec_entry = spec
            if isinstance(spec_entry, ResolvedSpec):
                spec = _unwrap_resolved_spec(spec_entry)
            # Swap to sub-agent's own spec so its harness drives the terminal auto-create.
            _sa_name_assign = body.get("sub_agent_name")
            if _sa_name_assign:
                from omnigent.runtime.workflow import _find_spec_by_name

                _sub_spec = _find_spec_by_name(spec, _sa_name_assign)
                if _sub_spec is not None:
                    spec = _sub_spec
                    spec_entry = (
                        ResolvedSpec(spec=spec, workdir=_resolved_spec_workdir(spec_entry))
                        if _resolved_spec_workdir(spec_entry) is not None
                        else spec
                    )
            harness_name = spec.executor.config.get("harness") or spec.executor.type
            harness_name = canonicalize_harness(harness_name) or harness_name

            # ── sys_agent_start policy gate ───────────────────────
            # Evaluate a synthetic ``sys_agent_start`` tool call so
            # policies like ``enforce_sandbox`` can inspect / override
            # sandbox config before the harness subprocess is created.
            #
            # Fires for BOTH top-level and sub-agent starts: the
            # sub-agent spec swap (line ~2665) happens before this
            # gate, so ``spec`` is already the child's spec when a
            # ``sub_agent_name`` is present.
            #
            # Why a synthetic tool instead of AP-server-side
            # enforcement?  ``sys_session_send`` (sub-agent spawn)
            # goes through AP-server policy, but its arguments carry
            # only ``(agent, title)`` — not the sandbox config.
            # Top-level starts have no tool call at all.  This gate
            # fills both gaps by carrying the sandbox dict and
            # evaluating via ``RunnerToolPolicyGate`` (same gate
            # that guards MCP tool calls) — no round-trip needed.
            _start_verdict = await _evaluate_agent_start_gate(spec, harness_name)
            if _start_verdict is not None:
                # ASK is collapsed to DENY: agent start is a
                # pre-spawn gate with no user interaction channel,
                # so we can't park and wait for approval.
                if _start_verdict.action in ("deny", "ask"):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "agent_start_denied",
                            "detail": _start_verdict.deny_text or "Agent start denied by policy",
                        },
                    )
                if _start_verdict.data is not None:
                    _apply_sandbox_override_from_verdict(spec, _start_verdict.data)

            spawn_env = _build_spawn_env_from_spec(
                spec,
                harness_name,
                workdir=_resolved_spec_workdir(spec_entry),
            )
            if harness_name == "claude-native" and spawn_env is None:
                from omnigent.claude_native_bridge import (
                    build_claude_native_spawn_env,
                )

                bridge_id = await _claude_native_bridge_id_for_session(
                    server_client=server_client,
                    session_id=session_id,
                )
                spawn_env = build_claude_native_spawn_env(session_id, bridge_id=bridge_id)
            if harness_name == "codex-native" and spawn_env is None:
                from omnigent.codex_native_bridge import (
                    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
                    build_codex_native_spawn_env,
                )

                labels = await _session_labels_for_runner_spawn(
                    server_client=server_client,
                    session_id=session_id,
                )
                bridge_id = labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY)
                spawn_env = build_codex_native_spawn_env(session_id, bridge_id=bridge_id)
            if harness_name == "pi-native" and spawn_env is None:
                from omnigent.pi_native_bridge import build_pi_native_spawn_env

                spawn_env = build_pi_native_spawn_env(session_id)
            if harness_name == "grok-native" and spawn_env is None:
                from omnigent.grok_native_bridge import build_grok_native_spawn_env

                spawn_env = build_grok_native_spawn_env(session_id)
            _session_spec_cache[session_id] = spec_entry
            from omnigent.llms.context_window import get_model_context_window
            from omnigent.runtime.workflow import _resolve_spec_model

            _model = _resolve_spec_model(spec)
            if _model:
                _ctx_window = get_model_context_window(_model)
                if _ctx_window is not None:
                    _compaction_contexts[session_id] = {
                        "context_window": _ctx_window,
                        "model": _model,
                        "config": spec.compaction,
                    }
        else:
            harness_name = "runner-test-default"
            spawn_env = None

        try:
            await process_manager.get_client(
                session_id,
                harness_name,
                env=spawn_env,
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "harness_spawn_failed",
                    "detail": _client_safe_error_detail(exc, context="harness spawn"),
                },
            )

        _session_start_cache[session_id] = time.time()
        # Don't replace queues ``stream_session`` or sub-agent delivery may have
        # already lazily created; that would orphan relays or undrained inbox
        # payloads.
        _sa_name = body.get("sub_agent_name")
        _ensure_session_coordination_state(
            session_id,
            agent_id=agent_id,
            sub_agent_name=_sa_name,
        )

        # Auto-bootstrap: if this is a claude-native session and no
        # terminal exists yet, create one. This handles the case
        # where a host-spawned runner receives a session assignment
        # without the CLI having created the terminal.
        if harness_name == "claude-native":
            # Serialize the check-and-create: a concurrent POST /v1/sessions
            # (from _on_runner_connect and the message path's relaunch
            # handshake both firing on the same connection) must not both
            # pass the "no terminal yet" test and double-launch. The second
            # caller in then sees the terminal the first created and no-ops.
            _ensure_lock = _claude_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_terminal = (
                    _tr is not None and _tr.get(session_id, "claude", "main") is not None
                )
                # An in-place agent switch BACK into claude-native (ran
                # claude-native, switched to another agent where turns were
                # added, then switched back) leaves the ORIGINAL claude
                # terminal registered — an open terminal tab keeps it alive.
                # Auto-create is skipped while a terminal exists, so the
                # re-synthesis from current AP items never runs and the agent
                # keeps its original on-disk transcript, missing the turns
                # added on the other agent. Confirmed in production: a switched-
                # back session showed external_session_id=None (rebuild never
                # ran) + the carry-history label set, resuming a transcript
                # without the away-agent's turns. When a post-switch rebuild is
                # pending (external_session_id cleared + carry-history stamped),
                # tear the stale terminal down so auto-create re-synthesizes.
                if _has_terminal and await _claude_native_session_wants_rebuild(
                    server_client, session_id
                ):
                    _logger.info(
                        "Claude terminal stale after agent switch; tearing it down to "
                        "rebuild from current items: session=%s",
                        session_id,
                    )
                    # Terminal-only teardown: drop the tmux pane + bridge but
                    # leave the session's primary OSEnv intact (cleanup_session
                    # would close the env mid-session and break the turn).
                    if _tr is not None:
                        await _tr.cleanup_conversation(session_id)
                    _has_terminal = False
                _logger.info(
                    "Claude terminal auto-create decision: session=%s terminal_registry=%s "
                    "has_existing_terminal=%s",
                    session_id,
                    _tr is not None,
                    _has_terminal,
                )
                # A /clear or /fork rotation binds the runner to the new
                # session before transferring the existing terminal onto it.
                # Auto-creating here would make that transfer 409 and loop
                # the rotation, so skip when the bridge's
                # active session still owns the terminal being transferred in.
                _terminal_inbound = False
                if not _has_terminal:
                    _terminal_inbound = await _claude_native_terminal_arrives_via_transfer(
                        server_client=server_client,
                        session_id=session_id,
                        resource_registry=resource_registry,
                    )
                    _logger.info(
                        "Claude terminal transfer-inbound check: session=%s terminal_inbound=%s",
                        session_id,
                        _terminal_inbound,
                    )
                if not _has_terminal and not _terminal_inbound:
                    # Resolve the session's agent spec so a bundle that ships a
                    # ``skills/`` directory is exposed to Claude Code via
                    # ``--plugin-dir`` (the CLI mirror of the SDK plugin
                    # wiring). Best-effort: a resolver error (HTTP failure,
                    # not-yet-bound agent) just means no bundled skills are
                    # wired — Claude still launches with its host config.
                    _native_bundle_dir: Path | None = None
                    _native_agent_name: str | None = None
                    _native_skills_filter: str | list[str] = "all"
                    try:
                        _native_spec = await _resolve_session_agent_spec(session_id)
                    except OmnigentError:
                        _native_spec = None
                        _logger.info(
                            "Claude terminal spec resolution failed; continuing without "
                            "bundle skills: session=%s",
                            session_id,
                        )
                    if _native_spec is not None:
                        _native_entry = _session_spec_cache.get(session_id)
                        _native_bundle_dir = (
                            _resolved_spec_workdir(_native_entry)
                            if _native_entry is not None
                            else None
                        )
                        _native_agent_name = getattr(_native_spec, "name", None)
                        _native_skills_filter = getattr(_native_spec, "skills_filter", "all")
                    # Auto-inject orchestrator skills (build-omnigent)
                    # into the bundle so Claude discovers them via
                    # --plugin-dir — mirrors _inject_orchestrator_skills
                    # in the load_skill dispatch path.
                    # When no bundle dir exists (single-YAML agents like
                    # claude-native-ui), create a synthetic bundle root in
                    # the session's bridge dir so the skill link +
                    # --plugin-dir still fires. Every omnigent agent
                    # should discover the platform skills without needing a
                    # bundled skills/ directory.
                    if _native_bundle_dir is None:
                        _native_bundle_dir = Path(
                            tempfile.mkdtemp(prefix="omnigent-skill-bundle-")
                        )
                    _logger.info(
                        "Claude terminal auto-create inputs resolved: session=%s "
                        "bundle_dir=%s agent_name=%s skills_filter=%s",
                        session_id,
                        _native_bundle_dir,
                        _native_agent_name,
                        _native_skills_filter,
                    )
                    _ensure_orchestrator_skills_in_bundle(_native_bundle_dir, _native_spec)
                    # Surface "terminal starting up" to the web UI before the
                    # (potentially slow) launch, and clear it in finally so a
                    # failure also drops the spinner rather than stranding it.
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_claude_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                            bundle_dir=_native_bundle_dir,
                            agent_name=_native_agent_name,
                            skills_filter=_native_skills_filter,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create claude terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Claude",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)
                elif _terminal_inbound:
                    _logger.info(
                        "Skipping claude terminal auto-create for %s; a sibling "
                        "session's terminal will transfer in (rotation target).",
                        session_id,
                    )

        if harness_name == "codex-native":
            # Same concurrency guard as the claude branch: two POST
            # /v1/sessions (connect callback + relaunch handshake) — or a
            # concurrent terminals-endpoint "ensure" — must not both pass
            # the check and double-launch. Reuses the lock the terminals
            # endpoint already keys on so both paths serialize per session.
            _codex_ensure_lock = _codex_terminal_ensure_locks.setdefault(
                session_id, asyncio.Lock()
            )
            async with _codex_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_codex_terminal = (
                    _tr is not None and _tr.get(session_id, "codex", "main") is not None
                )
                # Codex-native sessions use runner-owned app-server/TUI/forwarder
                # setup. The CLI now attaches to the resulting tmux terminal only.
                _needs_terminal = await _codex_session_needs_runner_terminal(
                    server_client, session_id
                )
                if not _has_codex_terminal and _needs_terminal:
                    # Resolve the session's bundle so its ``skills/`` are linked
                    # into the native Codex's CODEX_HOME (mirrors claude-native).
                    # Best-effort: a resolver error means no bundled skills.
                    _codex_bundle_dir: Path | None = None
                    _codex_skills_filter: str | list[str] = "all"
                    try:
                        _codex_spec = await _resolve_session_agent_spec(session_id)
                    except OmnigentError:
                        _codex_spec = None
                    if _codex_spec is not None:
                        _codex_entry = _session_spec_cache.get(session_id)
                        _codex_bundle_dir = (
                            _resolved_spec_workdir(_codex_entry)
                            if _codex_entry is not None
                            else None
                        )
                        _codex_skills_filter = getattr(_codex_spec, "skills_filter", "all")
                    # Auto-inject orchestrator skills into the codex
                    # bundle so CODEX_HOME/skills/ picks them up.
                    if _codex_bundle_dir is not None and _codex_spec is not None:
                        _ensure_orchestrator_skills_in_bundle(_codex_bundle_dir, _codex_spec)
                    # Surface "terminal starting up" to the web UI before the
                    # (potentially slow) launch, and clear it in finally so a
                    # failure also drops the spinner rather than stranding it.
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_codex_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            bundle_dir=_codex_bundle_dir,
                            skills_filter=_codex_skills_filter,
                            agent_spec=spec_entry,
                            server_client=server_client,
                            ensure_comment_relay=_ensure_comment_relay_started,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create codex terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Codex",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)
                elif not _needs_terminal:
                    _logger.info(
                        "Skipping codex terminal auto-create for %s; session "
                        "snapshot was not available.",
                        session_id,
                    )

        if harness_name == "pi-native":
            _pi_ensure_lock = _pi_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _pi_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_pi_terminal = (
                    _tr is not None and _tr.get(session_id, "pi", "main") is not None
                )
                if not _has_pi_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_pi_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create pi terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Pi",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        if harness_name == "grok-native":
            _grok_ensure_lock = _grok_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _grok_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_grok_terminal = (
                    _tr is not None and _tr.get(session_id, "grok", "main") is not None
                )
                if not _has_grok_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_grok_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create grok terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Grok",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        # Auto-bootstrap the Omnigent REPL terminal for non-native
        # (SDK-harness) top-level sessions: host the framework's own TUI
        # (``omnigent attach``) in a tmux pane so the web UI can embed it
        # — the SDK mirror of the claude-/codex-native terminals above.
        # Sub-agent sessions are skipped (their I/O surfaces through the
        # parent's transcript), as are the spec-less test scaffold and
        # runners wired without a terminal registry (nothing to host on).
        if (
            spec is not None
            and not is_native_harness(harness_name)
            and not _sa_name
            and resource_registry.terminal_registry is not None
        ):
            # Same double-launch hazard as the native branches: serialize
            # the check-and-create per session.
            _repl_lock = _repl_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _repl_lock:
                _tr = resource_registry.terminal_registry
                _has_repl_terminal = (
                    _tr.get(session_id, _REPL_TERMINAL_NAME, _REPL_TERMINAL_SESSION_KEY)
                    is not None
                )
                if not _has_repl_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_repl_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                        )
                    except Exception:
                        # Unlike the native branches, the REPL terminal is a
                        # secondary view — chat works without it — so a
                        # launch failure must not fail the session (no
                        # ``session.status: failed`` publication).
                        _logger.exception(
                            "Failed to auto-create omnigent REPL terminal for %s",
                            session_id,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        # Crash recovery (Step 8.5 Scenario A): if the session
        # has existing history, check whether the last item
        # indicates an incomplete turn that needs restarting.
        history = await _load_history_as_input(session_id)
        # Native terminal transcripts are mirrored from the underlying
        # runtime. A trailing user item can be a real failed/errored native
        # turn with no assistant item, not an unanswered Omnigent task to replay.
        if history and not is_native_harness(harness_name):
            _session_histories[session_id] = history
            last = history[-1]
            last_type = last.get("type")
            last_role = last.get("role")
            needs_turn = (
                (last_type == "message" and last_role == "user")
                or last_type == "function_call"
                or last_type == "function_call_output"
            )
            if needs_turn and session_id not in _active_turns:
                _active_turns[session_id] = None
                _publish_turn_status(session_id, "running")
                msg_body = {
                    "agent_id": agent_id,
                    "model": body.get("model", agent_id),
                }
                _turn_task = asyncio.create_task(
                    _run_turn_bg(msg_body, session_id),
                    name=f"turn-recover-{session_id}",
                )
                _active_turns[session_id] = _turn_task
                _turn_task.add_done_callback(
                    _background_tasks.discard,
                )
                _background_tasks.add(_turn_task)

        status = "running" if session_id in _active_turns else "idle"
        return JSONResponse(
            status_code=201,
            content={
                "id": session_id,
                "agent_id": agent_id,
                "status": status,
                "created_at": int(_session_start_cache[session_id]),
                "title": None,
                "labels": {},
                "runner_id": None,
                "reasoning_effort": None,
                "items": [],
                "permission_level": None,
            },
        )

