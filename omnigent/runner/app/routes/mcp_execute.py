    @app.post("/v1/sessions/{session_id}/mcp/execute")
    async def mcp_execute(session_id: str, request: Request) -> JSONResponse:
        """Execute a tool call on the runner after AP-server policy evaluation.

        Called by the Omnigent server's ``POST /v1/sessions/{id}/mcp`` handler
        **after** TOOL_CALL policy evaluation.  The Omnigent server owns policy
        enforcement (TOOL_CALL / TOOL_RESULT); the runner owns execution so
        that all tools run on the correct machine with the correct ``cwd``
        and environment.

        Handles **all** tool categories uniformly:

        - **MCP tools** (namespaced: ``server__tool``) — dispatched via
          :class:`RunnerMcpManager`, which manages live stdio subprocess
          connections to each configured MCP server.
        - **Runner-local tools** (bare names: ``sys_os_read``,
          ``sys_terminal_launch``, etc.) — dispatched via
          :func:`~omnigent.runner.tool_dispatch.execute_tool` using
          the session's terminal registry, inbox queue, and runner
          workspace.

        Supported ``method`` values:

        - ``tools/list`` — return namespaced MCP tool schemas for the
          agent's MCP servers (runner-local tool schemas are already
          injected by the Omnigent server in the turn request body).
        - ``tools/call`` — execute any tool call and return its output.

        Returns ``{"result": {"output": "..."}}`` on success or
        ``{"error": {"code": ..., "message": ...}}`` on failure.

        :param session_id: AP-allocated session id, e.g. ``"conv_abc123"``.
        :param request: FastAPI request; body must be a JSON object with
            ``"method"`` and ``"params"`` keys.
        :returns: :class:`JSONResponse` carrying result or error.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content={"error": {"code": -32700, "message": "Parse error: invalid JSON"}},
            )
        method: str = body.get("method") or ""
        params: dict[str, Any] = body.get("params") or {}

        if method == "tools/list":
            # Resolve the agent spec from the session cache, falling
            # back to the spec_resolver so the runner doesn't need a
            # separate spec-fetch round-trip for each tools/list call.
            if mcp_manager is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": -32000,
                            "message": "Runner MCP manager not configured",
                        }
                    },
                )
            spec_entry = _session_spec_cache.get(session_id)
            spec = _unwrap_resolved_spec(spec_entry)
            if spec is None and spec_resolver is not None:
                agent_id = _session_agent_ids.get(session_id)
                if agent_id:
                    try:
                        resolved = await spec_resolver(agent_id, session_id)
                        spec = _unwrap_resolved_spec(resolved)
                    except Exception:  # noqa: BLE001
                        pass
            if spec is None:
                return JSONResponse(
                    status_code=200,
                    content={
                        "error": {
                            "code": -32000,
                            "message": f"No spec available for session {session_id!r}",
                        }
                    },
                )
            try:
                result = await mcp_manager.schemas_for(spec)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    status_code=200,
                    content={
                        "error": {
                            "code": -32000,
                            "message": _client_safe_error_detail(exc, context="MCP tool dispatch"),
                        }
                    },
                )
            # Return schemas + failures so the Omnigent server can surface
            # partial results and per-server error hints.
            return JSONResponse(
                content={
                    "result": {
                        "schemas": result.schemas,
                        "tool_names": list(result.tool_names),
                        "failures": result.failures,
                    }
                }
            )

        if method == "tools/call":
            # params: {"name": "<server>__<tool>" or "sys_os_read", "arguments": {...}}
            # Namespaced names (``__`` present) are MCP tools dispatched via
            # RunnerMcpManager.  Bare names are runner-local tools (sys_*, terminal,
            # etc.) dispatched via execute_tool.
            import json as _json

            from omnigent.identity.signer import HEADER_NAME, decode_acting_identity
            from omnigent.runner.tool_dispatch import execute_tool

            # BDP-2422 Path A: the originating principal rides a signed header the
            # server attached; absent/invalid ⇒ None ⇒ today's behaviour.
            _acting_identity = decode_acting_identity(
                request.headers.get(HEADER_NAME), request.app.state.assertion_verifier
            )

            tool_name: str = params.get("name") or ""
            arguments: dict[str, Any] = params.get("arguments") or {}
            # MRTR retry: Omnigent server forwards inputResponses + requestState
            # after the user approved a gateway elicitation.
            input_responses: dict[str, Any] | None = params.get("inputResponses")
            request_state: str | None = params.get("requestState")
            if not tool_name:
                return JSONResponse(
                    status_code=200,
                    content={"error": {"code": -32000, "message": "Missing tool name"}},
                )

            if "__" in tool_name:
                # MCP tool: strip the namespace prefix and dispatch via RunnerMcpManager.
                # ``mcp_manager.call_tool`` expects the bare tool name that the MCP
                # server registered; the runner manager resolves the owning server by
                # scanning per-server tool lists internally.
                if mcp_manager is None:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "error": {
                                "code": -32000,
                                "message": "Runner MCP manager not configured",
                            }
                        },
                    )
                spec_entry = _session_spec_cache.get(session_id)
                spec = _unwrap_resolved_spec(spec_entry)
                if spec is None and spec_resolver is not None:
                    _agent_id = _session_agent_ids.get(session_id)
                    if _agent_id:
                        try:
                            resolved = await spec_resolver(_agent_id, session_id)
                            spec = _unwrap_resolved_spec(resolved)
                        except Exception:  # noqa: BLE001
                            pass
                if spec is None:
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": f"No spec available for session {session_id!r}",
                            }
                        },
                    )
                _server_prefix, _, bare_tool = tool_name.partition("__")
                try:
                    from omnigent.tools.mcp import McpElicitationRequired

                    if input_responses is not None:
                        # MRTR retry: the Omnigent server already showed the
                        # elicitation and gathered the user's response.
                        # Forward to the MCP server with inputResponses.
                        owning = mcp_manager._resolve_owning_server(spec, bare_tool)
                        if owning is None or owning.connection is None:
                            raise RuntimeError(
                                f"runner has no live MCP serving tool {bare_tool!r}"
                            )
                        output = await owning.connection.call_tool_with_elicitation(
                            bare_tool,
                            arguments,
                            input_responses=input_responses,
                            request_state=request_state,
                        )
                    else:
                        # BDP-2434: when the acting identity carries the user's
                        # subject_token, present an on-behalf-of bearer for this
                        # MCP egress (e.g. ByteDesk.Mcp). Absent ⇒ today's
                        # client_credentials connection, unchanged.
                        _subject_token = (
                            _acting_identity.subject_token
                            if _acting_identity is not None
                            else None
                        )
                        # BDP-2435: thread the acting agent's id as ``act_as`` so
                        # the OBO token's ``act_sub`` is THIS persona (None ⇒
                        # shared act_sub, unchanged).
                        _act_as_agent_id = (
                            _acting_identity.agent_id
                            if _acting_identity is not None
                            else None
                        )
                        output = await mcp_manager.call_tool(
                            spec,
                            bare_tool,
                            arguments,
                            session_id=session_id,
                            subject_token=_subject_token,
                            agent_id=_act_as_agent_id,
                        )
                except McpElicitationRequired as elicit:
                    # The external MCP server returned InputRequiredResult
                    # (MRTR). Pass it back to the Omnigent server so it can
                    # surface the elicitation via SSE and retry after
                    # the user responds.
                    return JSONResponse(
                        content={
                            "result": {
                                "input_required": {
                                    "inputRequests": elicit.input_requests,
                                    "requestState": elicit.request_state,
                                },
                            },
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": _client_safe_error_detail(
                                    exc, context="MCP tool dispatch"
                                ),
                            }
                        },
                    )
            else:
                # No double-underscore namespace prefix → runner-local tool
                # (sys_os_*, sys_terminal_*, etc.).  All MCP tools are
                # namespaced as ``{server}__{tool}`` by RunnerMcpManager, so
                # any name without ``__`` is definitively a runner-local tool.
                # Policy enforcement is handled by the AP server.
                spec_entry = _session_spec_cache.get(session_id)
                spec = _unwrap_resolved_spec(spec_entry)
                if spec is None and spec_resolver is not None:
                    _agent_id = _session_agent_ids.get(session_id)
                    if _agent_id:
                        try:
                            resolved = await spec_resolver(_agent_id, session_id)
                            spec = _unwrap_resolved_spec(resolved)
                        except Exception:  # noqa: BLE001
                            pass
                _agent_id_local = _session_agent_ids.get(session_id)
                _ensure_session_coordination_state(
                    session_id,
                    agent_id=_agent_id_local,
                )
                try:
                    output = await execute_tool(
                        tool_name=tool_name,
                        arguments=_json.dumps(arguments),
                        server_client=server_client,
                        terminal_registry=terminal_registry,
                        resource_registry=resource_registry,
                        agent_spec=spec,
                        conversation_id=session_id,
                        task_id=session_id,
                        agent_id=_agent_id_local,
                        agent_name=getattr(spec, "name", None),
                        runner_workspace=runner_workspace,
                        mcp_manager=None,
                        session_inbox=_session_inboxes.get(session_id),
                        session_async_tasks=_session_async_tasks.get(session_id),
                        harness_client=None,
                        publish_event=_publish_event,
                        filesystem_registry=filesystem_registry,
                        acting_identity=_acting_identity,
                    )
                except Exception as exc:  # noqa: BLE001
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": _client_safe_error_detail(
                                    exc, context="MCP tool dispatch"
                                ),
                            }
                        },
                    )
            return JSONResponse(content={"result": {"output": output}})

        return JSONResponse(
            status_code=200,
            content={"error": {"code": -32601, "message": f"Method not found: {method!r}"}},
        )

    def _resolve_summarize_connection(
        session_id: str,
        model: str,
    ) -> dict[str, str] | None:
        """
        Resolve LLM connection for ``/v1/summarize`` from the session's spec.

        Mirrors the harness auth resolution order so compaction
        summarization uses the same credentials as normal agent turns:

        1. :class:`ProviderAuth` — resolve named provider from
           ``~/.omnigent/config.yaml``, extract ``api_key`` + ``base_url``
           from the ``openai`` family.
        2. :class:`DatabricksAuth` — resolve the named profile from
           ``~/.databrickscfg`` into ``base_url`` + ``api_key``.
        3. :class:`ApiKeyAuth` — inline ``api_key`` and optional
           ``base_url``.
        4. Global config ``auth:`` block (when spec declares no auth).
        5. Legacy ``executor.config["profile"]`` or auto-Databricks
           DEFAULT for ``databricks-*`` model prefixes.

        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param model: LLM model string used to decide whether to
            attempt Databricks profile resolution, e.g.
            ``"databricks/databricks-gpt-5-5"``.
        :returns: A connection dict with ``"base_url"`` and ``"api_key"``
            keys, or ``None`` when no credentials could be resolved.
        """
        from omnigent.spec.types import ApiKeyAuth, DatabricksAuth, ProviderAuth

        spec_entry = _session_spec_cache.get(session_id)
        if spec_entry is None:
            return None
        spec = spec_entry.spec if hasattr(spec_entry, "spec") else spec_entry
        if spec is None:
            return None

        auth = getattr(spec.executor, "auth", None)

        # 1. ProviderAuth → resolve named provider, extract openai family.
        if isinstance(auth, ProviderAuth):
            return _resolve_provider_connection(auth.name, model)

        # 2. DatabricksAuth → resolve profile from ~/.databrickscfg.
        if isinstance(auth, DatabricksAuth):
            return _resolve_databricks_connection(auth.profile, session_id)

        # 3. ApiKeyAuth → inline key + optional base_url.
        if isinstance(auth, ApiKeyAuth):
            conn: dict[str, str] = {"api_key": auth.api_key}
            if auth.base_url:
                conn["base_url"] = auth.base_url
            return conn

        # 4. Global config auth (when spec declares no auth at all).
        _spec_has_legacy_profile = bool(
            spec.executor.profile or (spec.executor.config or {}).get("profile")
        )
        if auth is None and not _spec_has_legacy_profile:
            from omnigent.runtime.workflow import _load_global_auth

            global_auth = _load_global_auth()
            if isinstance(global_auth, DatabricksAuth):
                return _resolve_databricks_connection(global_auth.profile, session_id)
            if isinstance(global_auth, ApiKeyAuth):
                conn = {"api_key": global_auth.api_key}
                if global_auth.base_url:
                    conn["base_url"] = global_auth.base_url
                return conn

        # 5. Legacy fallback: executor.config.profile, executor.profile,
        #    or auto-Databricks DEFAULT for databricks-* models.
        if model.startswith(("databricks/", "databricks-")):
            _db_profile = (
                spec.executor.profile or (spec.executor.config or {}).get("profile") or "DEFAULT"
            )
            return _resolve_databricks_connection(_db_profile, session_id)

        return None

    def _resolve_provider_connection(
        provider_name: str,
        model: str = "",
    ) -> dict[str, str] | None:
        """
        Resolve connection from a named provider's family.

        Loads providers from ``~/.omnigent/config.yaml`` and extracts
        ``api_key`` + ``base_url`` from the matching family entry.
        Tries the ``anthropic`` family for ``anthropic/`` or
        ``claude`` models, otherwise ``openai``. Returns ``None``
        when the provider or a suitable family is not configured.

        :param provider_name: Provider name from the ``providers:``
            block, e.g. ``"litellm"`` or ``"openrouter"``.
        :param model: LLM model string used to select the family,
            e.g. ``"anthropic/claude-sonnet-4-20250514"``.
        :returns: A connection dict, or ``None``.
        """
        try:
            from omnigent.onboarding.detected import effective_config_with_detected
            from omnigent.onboarding.provider_config import (
                load_config,
                load_providers,
            )

            config = load_config()
            providers = load_providers(effective_config_with_detected(config))
            entry = providers.get(provider_name)
            if entry is None:
                return None
            # Databricks-kind providers route through profile resolution.
            if entry.kind == "databricks" and entry.profile:
                return _resolve_databricks_connection(entry.profile, provider_name)
            # Pick the family matching the model prefix; fall back to
            # whichever family the provider has.
            _is_anthropic = model.startswith(("anthropic/", "claude"))
            _preferred = "anthropic" if _is_anthropic else "openai"
            _fallback = "openai" if _is_anthropic else "anthropic"
            family = entry.family(_preferred) or entry.family(_fallback)
            if family is None:
                return None
            conn: dict[str, str] = {}
            if family.api_key:
                conn["api_key"] = family.api_key
            if family.base_url:
                conn["base_url"] = family.base_url
            return conn or None
        except Exception:  # noqa: BLE001
            _logger.warning(
                "/v1/summarize: failed to resolve provider %r",
                provider_name,
                exc_info=True,
            )
            return None

    def _resolve_databricks_connection(
        profile: str,
        context: str,
    ) -> dict[str, str] | None:
        """
        Resolve Databricks credentials from a ``~/.databrickscfg`` profile.

        :param profile: Databricks profile name, e.g. ``"oss"`` or
            ``"DEFAULT"``.
        :param context: Logging context (session_id or provider name).
        :returns: A connection dict with ``"base_url"`` and ``"api_key"``,
            or ``None`` on failure.
        """
        from omnigent.runtime.credentials.databricks import (
            resolve_databricks_workspace,
        )

        try:
            creds = resolve_databricks_workspace(profile)
        except OSError:
            _logger.warning(
                "/v1/summarize: failed to resolve Databricks profile %r (context=%s)",
                profile,
                context,
                exc_info=True,
            )
            return None
        return {
            "base_url": creds.host.rstrip("/") + "/serving-endpoints",
            "api_key": creds.token,
        }

