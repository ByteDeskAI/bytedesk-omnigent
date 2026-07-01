    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> JSONResponse:
        """
        End a session on this runner.

        Cancels any active turn, closes SSE subscriptions, releases
        the harness subprocess, and cleans up runner-local caches
        and resources (environments, terminals).

        Per ``designs/SESSION_REARCHITECTURE.md`` §4 step 3.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: Deletion confirmation JSON.
        """
        # Cancel active turn before releasing harness.
        turn_task = _active_turns.pop(session_id, None)
        if turn_task is not None and isinstance(turn_task, asyncio.Task):
            turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn_task
        _session_message_buffers.pop(session_id, None)
        _ingest_next_seq.pop(session_id, None)
        _ingest_now_serving.pop(session_id, None)
        _ingest_cond.pop(session_id, None)
        _codex_terminal_ensure_locks.pop(session_id, None)
        _claude_terminal_ensure_locks.pop(session_id, None)
        _pi_terminal_ensure_locks.pop(session_id, None)
        _grok_terminal_ensure_locks.pop(session_id, None)
        _repl_terminal_ensure_locks.pop(session_id, None)
        _interrupted_sessions.discard(session_id)

        if process_manager is not None:
            await process_manager.forward_cancel(session_id)

        # Signal end-of-stream to GET /stream subscriber.
        queue = _session_event_queues.get(session_id)
        if queue is not None:
            queue.put_nowait(None)

        await resource_registry.cleanup_session(session_id)

        if process_manager is not None:
            await process_manager.release(session_id)

        _session_spec_cache.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_start_cache.pop(session_id, None)
        _session_workspace_cache.pop(session_id, None)
        _session_snapshot_cache.pop(session_id, None)
        _session_snapshot_locks.pop(session_id, None)
        _session_spec_locks.pop(session_id, None)
        _session_fs_registries.pop(session_id, None)
        _session_agent_ids.pop(session_id, None)
        _session_tool_schemas.pop(session_id, None)
        if _relay := _session_comment_relays.pop(session_id, None):
            _relay.close()
        _session_histories.pop(session_id, None)
        _compaction_contexts.pop(session_id, None)
        _last_server_item_id.pop(session_id, None)
        _loaded_server_item_ids.pop(session_id, None)
        _session_event_queues.pop(session_id, None)
        _session_inboxes.pop(session_id, None)
        _subagent_wake_pending.discard(session_id)
        # Without this, a deleted child's name lingers, so a late terminal
        # status for it reads is_runner_known_subagent=True with no work
        # entry → a spurious 503 subagent_delivery_not_confirmed (AP retries)
        # plus an unbounded leak across deleted sessions.
        _session_sub_agent_names.pop(session_id, None)
        # Drop the child→parent fan-out mapping if this session was a
        # spawned sub-agent child (no-op otherwise).
        unregister_child_session(session_id)
        unregister_subagent_work_for_session(session_id)
        if filesystem_registry is not None:
            filesystem_registry.unregister_conversation(session_id)
        for _task, evt in _session_async_tasks.pop(session_id, {}).values():
            evt.set()
        for _tmr in _session_timers.pop(session_id, {}).values():
            _tmr.cancel()
        _version_cache.pop(session_id, None)
        # Clean up any response_id → conversation_id mappings
        # for this session.
        stale_resp_ids = [rid for rid, cid in _resp_to_conv.items() if cid == session_id]
        for rid in stale_resp_ids:
            _resp_to_conv.pop(rid, None)

        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "object": "session.deleted",
                "deleted": True,
            },
        )

    async def _load_history_as_input(
        session_id: str,
        drop_item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Load conversation history from the server and convert to
        the harness input format.

        Fetches items via ``GET /v1/sessions/{id}/items`` and maps
        each to the Responses-API input shape that the harness
        adapter's ``_translate_input_to_messages`` understands.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param drop_item_id: When set, the raw store item with this
            id is excluded before conversion, e.g.
            ``"item_abc123"``. Used by the cold-cache rehydration
            path to drop this turn's just-persisted (pre-resolution)
            input so the caller can append its own resolved copy
            without duplication. ``None`` keeps every item.
        :returns: List of input items in chronological order, or
            empty list if the fetch fails. Each item is a dict
            like ``{"type": "message", "role": "user",
            "content": [...]}``.
        """
        # Paginate through all items using cursor-based `after`.
        all_items: list[dict[str, Any]] = []
        after_cursor: str | None = None
        while True:
            params: dict[str, str] = {
                "limit": "100",
                "order": "asc",
            }
            if after_cursor is not None:
                params["after"] = after_cursor
            try:
                resp = await server_client.get(
                    f"/v1/sessions/{session_id}/items",
                    params=params,
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    _logger.warning(
                        "History load returned %d for session=%s",
                        resp.status_code,
                        session_id,
                    )
                    break
            except httpx.HTTPError:
                _logger.warning(
                    "History load failed for session=%s",
                    session_id,
                    exc_info=True,
                )
                break
            page = resp.json()
            page_items = page.get("data", [])
            if not page_items:
                break
            _remember_loaded_server_item_ids(session_id, page_items)
            all_items.extend(page_items)
            # Track last item ID for incremental catch-up.
            last_id = page_items[-1].get("id")
            if last_id:
                _last_server_item_id[session_id] = last_id
            if not page.get("has_more", False):
                break
            after_cursor = last_id

        if drop_item_id is not None:
            all_items = [it for it in all_items if it.get("id") != drop_item_id]

        return _convert_raw_items_to_input(all_items)

    def _convert_raw_items_to_input(
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Convert raw server items to harness input format.

        Scans for the latest ``compaction`` item and discards
        everything before it — those items are already summarized.
        The compaction item is expanded into a synthetic
        user+assistant pair carrying the summary text.

        :param items: Raw items from GET /v1/sessions/{id}/items.
        :returns: List of harness-input-shaped dicts.
        """
        compaction_idx: int | None = None
        for i, item in enumerate(items):
            if item.get("type") == "compaction":
                compaction_idx = i

        result: list[dict[str, Any]] = []
        if compaction_idx is not None:
            c = items[compaction_idx]
            result.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "[Automatically generated summary of prior "
                                "conversation context.]\n\n"
                                "Please provide a summary of our conversation so far."
                            ),
                        }
                    ],
                }
            )
            result.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": c.get("summary", ""),
                        }
                    ],
                }
            )
            remaining = items[compaction_idx + 1 :]
        else:
            remaining = items

        _skipped_types: list[str] = []
        for item in remaining:
            item_type = item.get("type")
            if item_type not in ("message", "function_call", "function_call_output"):
                _skipped_types.append(str(item_type))
            if item_type == "message":
                result.append(
                    {
                        "type": "message",
                        "role": item.get("role", "user"),
                        "content": item.get("content", []),
                    }
                )
            elif item_type == "function_call":
                result.append(
                    {
                        "type": "function_call",
                        "call_id": item.get("call_id"),
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                    }
                )
            elif item_type == "function_call_output":
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.get("call_id"),
                        "output": item.get("output"),
                    }
                )
        if _skipped_types:
            _logger.warning(
                "_convert_raw_items_to_input: skipped %d items with types: %s",
                len(_skipped_types),
                _skipped_types,
            )
        _logger.info(
            "_convert_raw_items_to_input: %d raw items → %d converted (compaction_idx=%s)",
            len(items),
            len(result),
            compaction_idx,
        )
        return result

    def _extract_last_assistant_text(session_id: str) -> str:
        """
        Extract the text of the last assistant message from
        in-memory history.

        Used by sub-agent dispatch to collect the child turn's
        output when the Future is resolved.

        :param session_id: Session/conversation ID whose history
            to search, e.g. ``"conv_child123"``.
        :returns: The assistant message text, or an empty string
            if no assistant message is found.
        """
        history = _session_histories.get(session_id, [])
        for item in reversed(history):
            if item.get("role") == "assistant":
                content = item.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text") or block.get("input_text")
                            if text:
                                parts.append(str(text))
                        elif isinstance(block, str):
                            parts.append(block)
                    return "\n".join(parts) if parts else ""
        return ""

    def _serialize_messages_as_summary(
        messages: list[dict[str, Any]],
    ) -> str:
        """
        Serialize a compacted message list into a text summary.

        Used as the compaction item's ``summary`` field when Layer 1
        (LLM summarization) fails and Layer 2 (truncation) produces
        the result. The serialized text is rougher than an LLM
        summary but preserves the conversation content so the LLM
        can pick up context on reload.

        :param messages: The compacted message list from
            ``compact()``.
        :returns: A text representation of the messages.
        """
        parts: list[str] = []
        for msg in messages:
            msg_type = msg.get("type", "")
            if msg_type == "message":
                role = msg.get("role", "unknown")
                content = msg.get("content", [])
                text_parts: list[str] = []
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            t = block.get("text") or block.get("input_text") or ""
                            if t:
                                text_parts.append(str(t))
                        elif isinstance(block, str):
                            text_parts.append(block)
                text = "\n".join(text_parts) if text_parts else "(no text)"
                parts.append(f"[{role}]: {text}")
            elif msg_type == "function_call":
                name = msg.get("name", "unknown")
                parts.append(f"[tool call]: {name}")
            elif msg_type == "function_call_output":
                output = msg.get("output", "")
                if len(str(output)) > 200:
                    output = str(output)[:200] + "..."
                parts.append(f"[tool result]: {output}")
        return "\n\n".join(parts)

    async def _proactive_compact_if_needed(
        conv: str,
        cc: dict[str, Any],
        spec: Any | None,
    ) -> None:
        """
        Run proactive compaction if the history exceeds the token budget.

        Checks the estimated token count of ``_session_histories[conv]``
        against ``trigger_threshold * context_window``. If over budget,
        runs the layered ``compact()`` function and replaces the
        in-memory history with the compacted version. Publishes
        compaction SSE events (in_progress / compaction / completed)
        to the session event queue.

        :param conv: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param cc: Compaction context dict with ``context_window``,
            ``model``, and ``config`` keys.
        :param spec: The cached ``AgentSpec``, or ``None``.
        """
        from omnigent.runtime.compaction import (
            CompactionResult,
            compact,
            count_tokens,
        )

        context_window: int = cc["context_window"]
        model: str = cc["model"]
        compaction_config = cc.get("config")
        threshold = compaction_config.trigger_threshold if compaction_config else 0.8
        budget = int(context_window * threshold)
        messages = _session_histories[conv]
        # Prefer provider-reported usage when available — tiktoken
        # underestimates for harness executors whose internal session
        # is larger than what the runner persists.
        provider_tokens: int | None = cc.get("provider_tokens")
        if provider_tokens is not None:
            estimated = provider_tokens
        else:
            estimated = count_tokens(messages, model)
        _logger.info(
            "Compaction check: conv=%s estimated=%d budget=%d msgs=%d provider=%s",
            conv,
            estimated,
            budget,
            len(messages),
            provider_tokens,
        )
        if estimated <= budget:
            return

        _logger.info(
            "Proactive compaction for session=%s: %d tokens > %d budget",
            conv,
            estimated,
            budget,
        )
        _publish_event(
            conv,
            {
                "type": "response.compaction.in_progress",
                "session_id": conv,
            },
        )

        try:
            from omnigent.entities import ConversationItem, MessageData

            history_items = [
                ConversationItem(
                    id=f"synthetic_{i}",
                    type="message",
                    status="completed",
                    response_id="",
                    created_at=0,
                    data=MessageData(
                        role=m.get("role", "user"),
                        content=m.get("content", []),
                        **({"agent": cc["model"]} if m.get("role") == "assistant" else {}),
                    ),
                )
                for i, m in enumerate(messages)
                if m.get("type") == "message"
            ]

            connection: dict[str, str] | None = None
            if spec and spec.executor.config.get("connection"):
                connection = spec.executor.config["connection"]

            if connection is None:
                connection = _resolve_summarize_connection(conv, model)

            llm_client = _get_runner_llm_client()
            result: CompactionResult = await compact(
                messages,
                history_items,
                config=compaction_config,
                context_window=context_window,
                system_token_budget=0,
                model=model,
                task_id=conv,
                llm_client=llm_client,
                connection=connection,
            )
            _session_histories[conv] = result.messages
            # Invalidate stale provider tokens — the context was
            # just compacted so the old value no longer reflects
            # reality.  The next response.completed will set a
            # fresh value.
            cc.pop("provider_tokens", None)

            # Always persist a compaction item — regardless of
            # which layer produced the result. If Layer 1 (LLM
            # summary) succeeded, use the summary text. If it
            # failed and Layer 2 (truncation) fired, serialize the
            # truncated messages as the summary so the boundary is
            # durable across restarts.
            if result.summary_metadata is not None:
                meta = result.summary_metadata
                summary_text = meta.text
                summary_model = meta.model
                summary_tokens = meta.token_count
                last_item_id = meta.last_item_id
            else:
                summary_text = _serialize_messages_as_summary(
                    result.messages,
                )
                summary_model = model
                from omnigent.runtime.compaction import count_tokens

                summary_tokens = count_tokens(
                    result.messages,
                    model,
                )
                last_item_id = _last_server_item_id.get(conv)
                if not last_item_id:
                    # No real server-side item ID available. Skip
                    # persisting — a compaction item with a synthetic
                    # or unknown last_item_id would poison the history
                    # cursor on the server (after="synthetic_N" returns
                    # nothing, so future turns see empty history and
                    # compaction never triggers again).
                    _logger.warning(
                        "Skipping compaction persist for %s: no server-side "
                        "last_item_id available (Layer 2 failed, no items "
                        "fetched from server yet)",
                        conv,
                    )
                    return
            compaction_event = {
                "type": "compaction",
                "summary": summary_text,
                "last_item_id": last_item_id,
                "model": summary_model,
                "token_count": summary_tokens,
            }
            # Persist directly to the server — do NOT also
            # _publish_event with type="compaction" because the
            # relay would extract and persist a duplicate.
            try:
                await server_client.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "compaction",
                        "data": compaction_event,
                    },
                    timeout=10.0,
                )
            except (httpx.HTTPError, RuntimeError):
                _logger.warning(
                    "Failed to persist compaction item for %s",
                    conv,
                    exc_info=True,
                )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Proactive compaction failed for session=%s",
                conv,
                exc_info=True,
            )
        finally:
            _publish_event(
                conv,
                {
                    "type": "response.compaction.completed",
                    "session_id": conv,
                },
            )

    _CANCELLATION_TOOL_OUTPUT = "[Cancelled — tool execution was interrupted.]"
    # Tells the model the prior request was abandoned, not just that the
    # assistant's reply was cut off — otherwise the canceled instruction
    # survives in history and the next turn acts on it (issue: cancel-leak).
    _CANCELLATION_MARKER_TEXT = (
        "[System: interrupted]\n"
        "The user interrupted and abandoned their previous request (the user "
        "message immediately before this one). Do not resume or act on that "
        "interrupted request unless the user asks for it again; treat the next "
        "user message as the current instruction. The preceding assistant "
        "message may be incomplete."
    )

    def _append_cancellation_items(conv_id: str) -> None:
        """Insert synthetic items for an interrupted turn.

        1. Synthetic ``function_call_output`` for every dangling
           ``function_call`` (call emitted but no matching output).
        2. A cancellation marker ``message`` so the LLM knows
           the prior output was incomplete.

        Items are appended to the runner's in-memory
        ``_session_histories`` and POSTed to the server for
        database persistence.

        .. todo::
            Phase 2 — flush *partial* content on interrupt:
            • Join accumulated ``_text_acc`` deltas and persist
              as an assistant message with
              ``status="incomplete"`` on ``ConversationItem``.
            • Persist in-flight function_call items with
              ``status="incomplete"``.
            • Persist partial tool outputs with
              ``status="incomplete"``.
        """
        history = _session_histories.get(conv_id, [])

        call_ids_with_output: set[str] = set()
        dangling_calls: list[dict[str, Any]] = []
        for item in history:
            itype = item.get("type")
            if itype == "function_call":
                cid = item.get("call_id")
                if cid:
                    dangling_calls.append(item)
            elif itype == "function_call_output":
                cid = item.get("call_id")
                if cid:
                    call_ids_with_output.add(cid)

        items_to_persist: list[dict[str, Any]] = []
        synthetic_items: list[dict[str, Any]] = []
        cached_spec_entry = _session_spec_cache.get(conv_id)
        cached_spec = _unwrap_resolved_spec(cached_spec_entry)
        agent_name = cached_spec.name if cached_spec else "unknown"
        for fc in dangling_calls:
            call_id = fc["call_id"]
            if call_id not in call_ids_with_output:
                fc_for_db = dict(fc)
                fc_for_db.setdefault("agent", agent_name)
                items_to_persist.append(fc_for_db)
                synthetic_output = {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _CANCELLATION_TOOL_OUTPUT,
                }
                synthetic_items.append(synthetic_output)
                items_to_persist.append(synthetic_output)

        marker = {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": _CANCELLATION_MARKER_TEXT,
                }
            ],
        }
        synthetic_items.append(marker)
        items_to_persist.append(marker)

        # Only the synthetic items go into in-memory history — the
        # dangling function_calls are already there from proxy_stream.
        _session_histories.setdefault(conv_id, []).extend(synthetic_items)

        loop = asyncio.get_running_loop()
        _task = loop.create_task(
            _persist_cancellation_items(conv_id, items_to_persist),
            name=f"persist-cancel-{conv_id}",
        )
        _task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_task)

    async def _persist_cancellation_items(
        conv_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        """POST synthetic cancellation items to the server.

        Uses the ``external_conversation_item`` event type so the
        server persists without forwarding back to the runner.
        """
        import uuid as _uuid

        response_id = f"cancel_{_uuid.uuid4().hex}"
        for item in items:
            item_type = item.get("type", "message")
            item_data = {k: v for k, v in item.items() if k != "type"}
            try:
                await server_client.post(
                    f"/v1/sessions/{conv_id}/events",
                    json={
                        "type": "external_conversation_item",
                        "data": {
                            "item_type": item_type,
                            "item_data": item_data,
                            "response_id": response_id,
                        },
                    },
                    timeout=10.0,
                )
            except (httpx.HTTPError, RuntimeError):
                _logger.warning(
                    "Failed to persist cancellation item for %s: %s",
                    conv_id,
                    item_type,
                    exc_info=True,
                )

    async def _recover_sub_agent_name(conv_id: str) -> str | None:
        """Resolve a session's sub-agent name, recovering it if lost.

        The in-memory ``_session_sub_agent_names`` map is populated only on
        ``POST /v1/sessions`` and wiped on a runner restart / cleared on
        session delete. A continuation turn that reaches a harness-resolution
        path after a tunnel reconnect therefore finds it empty and resolves
        the PARENT harness for a child session — respawning the harness and
        tearing down the child's native terminal ("Bridge closed").

        This recovers the identity from the authoritative server snapshot
        (``GET /v1/sessions/{id}`` -> ``sub_agent_name``) and backfills the
        in-memory map so subsequent reads are cheap. Best-effort: a failed
        lookup returns ``None`` (a top-level session, or the snapshot is
        unavailable), preserving the prior behavior.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :returns: The sub-agent name, or ``None`` for a top-level session
            (or when it cannot be resolved).
        """
        cached = _session_sub_agent_names.get(conv_id)
        if cached:
            return cached
        try:
            snapshot = await _session_snapshot(conv_id)
        except Exception:  # noqa: BLE001 — best-effort recovery
            return None
        name = snapshot.sub_agent_name if snapshot is not None else None
        if name:
            _session_sub_agent_names[conv_id] = name
        return name

    def _session_harness_name(conv_id: str) -> str | None:
        """
        Resolve the canonical harness name for a session, if known.

        Reads ``_session_spec_cache`` (populated at session start by
        ``POST /v1/sessions/{conv}/start`` and the spawn dispatch path)
        and re-derives the harness name via the same precedence used
        at spawn time: ``executor.config.harness`` first, then
        ``executor.type``, then canonicalized via
        :func:`canonicalize_harness`.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The canonical harness name (e.g. ``"claude-native"``)
            or ``None`` if no spec is cached for this session.
        """
        spec = _session_spec_cache.get(conv_id)
        if spec is None:
            return None
        h = spec.executor.config.get("harness") or spec.executor.type
        return canonicalize_harness(h) or h

    def _publish_turn_status(
        conv_id: str,
        status: str,
        error: dict[str, Any] | None = None,
    ) -> None:
        """
        Publish a turn-lifecycle ``session.status`` edge unless a native
        terminal observer already owns that edge.

        Terminal-backed sessions do not all have the same safe edge source.
        For claude-native, the PTY-activity watcher owns ``running`` and
        ``idle`` because a runner turn only types into Claude Code's pane.
        For codex-native, the runner may publish ``running`` when it accepts
        a web turn for dispatch, but the Codex app-server forwarder owns
        ``idle`` because the runner's injection task returns as soon as Codex
        accepts the message, while the user-visible model turn may still be
        active.

        ``failed`` always publishes: a turn-setup error is not observable
        from terminal activity and must surface regardless of harness.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param status: The status edge, ``"running"`` / ``"idle"`` /
            ``"failed"``.
        :param error: Failure detail dict for a ``"failed"`` edge, carried
            through so a SETUP-phase failure surfaces a real message;
            ``None`` for ``running`` / ``idle``.
        :returns: None.
        """
        # An unresolved spec (``_session_harness_name`` → ``None``) means the
        # session hasn't resolved a terminal-backed harness yet, so no native
        # observer is known and the turn lifecycle is still the only status
        # source — fall through and publish. Suppress only once we positively
        # know the harness/edge is terminal-owned.
        harness = _session_harness_name(conv_id)
        if status != "failed" and harness in {"claude-native", "pi-native"}:
            return
        if status == "idle" and harness == "codex-native":
            return
        event: dict[str, Any] = {"type": "session.status", "status": status}
        if error is not None:
            event["error"] = error
        _publish_event(conv_id, event)

    def _is_native_harness(conv_id: str) -> bool:
        """
        Whether this session types messages directly into a terminal.

        Native harnesses (``claude-native`` / ``codex-native`` /
        ``pi-native``) have
        *instant* turns — ``run_turn`` returns as soon as the message is
        typed into the pane — and type only the latest user message per
        turn. The runner's mid-turn forward + collapse-batch continuation,
        designed for LLM harnesses whose turns have real duration, drop
        and duplicate messages for them (the forward's injection races the
        instant turn's teardown; the collapse types only the last buffered
        message). Native sessions therefore take the no-forward,
        one-message-at-a-time delivery path. See
        ``designs/RUNNER_MESSAGE_INGEST.md`` Part C.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: ``True`` for native terminal sessions.
        """
        return is_native_harness(_session_harness_name(conv_id))

    def _wake_parent_after_native_interrupt(conv_id: str) -> None:
        """Mark an interrupted native sub-agent cancelled and wake its parent.

        Shared by the claude/codex native interrupt handlers; a no-op when
        *conv_id* is a top-level session (no one's tracked sub-agent).

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        """
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent interrupted]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "Native interrupt: sub-agent delivery not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )

    async def _handle_claude_native_interrupt(conv_id: str) -> Response:
        """
        Stop a claude-native session by injecting Escape into tmux.

        Claude-native sessions have no in-flight harness turn for the
        scaffold's ``InterruptEvent`` path to cancel — the harness's
        ``run_turn`` returns as soon as the user prompt is pasted
        into the tmux pane, and the actual long-running work (Claude
        generating a response) happens inside the ``claude`` binary
        in the pane. The only way to stop it is sending a key to the
        terminal.

        Sending the Escape is the whole job — no synthetic
        ``[System: interrupted]`` transcript marker is persisted. That
        marker exists for in-process LLM harnesses, where the runner's
        ``_session_histories`` *is* the model's next-turn context, so a
        cut-off turn must be repaired (dangling ``function_call`` items
        get synthetic outputs) and annotated. None of that applies to
        Claude-native: Claude owns its own session, the runner only types
        the latest user message into the pane, and Claude records the
        interrupt in its own transcript (mirrored by the forwarder). The
        web UI's interrupt decoration comes from the harness-agnostic
        ``session.interrupted`` event, not this marker. Persisting it here
        only forged a ``role:"user"`` bubble the user never sent into the
        AP-side mirror, diverging it from Claude's real transcript.

        Status is intentionally NOT synthesized here. The terminal's PTY
        activity watcher is the single source of truth: it emits
        ``session.status: idle`` once the pane quiesces after the Escape,
        and keeps the session ``running`` if the interrupt didn't actually
        stop Claude. Emitting ``idle`` here too (as this used to, back when
        the hook-based status couldn't observe idle-on-Escape) would
        bypass — and desync — the watcher's running/idle dedupe, and could
        strand the UI on ``idle`` while Claude kept working.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: 204 on success. 503 if the tmux target is not yet
            advertised (caller treats this as a best-effort failure).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_interrupt,
        )

        # Resolve the bridge id from the session's labels so
        # ``--resume`` sessions (where bridge_id != conversation_id)
        # land in the right tmux pane. Falls back to ``conv_id`` for
        # legacy single-session bridges; see
        # :func:`_claude_native_bridge_id_for_session`.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: UI stop must feel snappy; a missing
            # tmux.json means there's nothing to interrupt anyway.
            await asyncio.to_thread(inject_interrupt, bridge_dir, timeout_s=1.0)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native interrupt"),
                },
            )
        # No ``_append_cancellation_items``: the synthetic marker is for
        # in-process LLM harnesses only (see docstring). The /events dispatch
        # already keeps native out of ``_interrupted_sessions``.
        # NB: no synthesized ``session.status: idle`` here — the PTY watcher
        # emits idle when the pane quiesces after the Escape (and re-asserts
        # running if the interrupt didn't take). See the docstring.
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _handle_codex_native_interrupt(conv_id: str) -> Response:
        """
        Stop a codex-native turn via Codex app-server ``turn/interrupt``.

        Codex's own TUI maps its interrupt key to an app-server request
        carrying the active ``threadId`` and ``turnId``. The web/runner path
        should use that protocol directly instead of guessing at terminal
        keybindings: the Codex app-server validates that the requested turn is
        active and replies after the turn aborts.

        No interrupted marker is synthesized here. Codex records the interrupt
        only as a turn-status edge in its own transcript — not as a message — so
        injecting a ``[System: interrupted]`` bubble into the Omnigent mirror would
        diverge the web UI from Codex's actual session (and never survive a
        ``--resume``). Interruption surfaces via the harness-agnostic
        ``session.interrupted`` event; a durable, faithful indicator is a
        follow-up (persist turn status, render from that — no fabricated
        message). claude-native is unaffected: its badge mirrors Claude Code's
        *own* ``[Request interrupted by user]`` record, which is real.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 204 when no active turn is recorded or the interrupt lands;
            503 when Codex rejects the active-turn interrupt.
        """
        from omnigent.codex_native_app_server import client_for_transport
        from omnigent.codex_native_bridge import (
            CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
            bridge_dir_for_bridge_id,
            read_bridge_state,
        )

        labels = await _session_labels_for_runner_spawn(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_id = labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY) or conv_id
        state = read_bridge_state(bridge_dir_for_bridge_id(bridge_id))
        if state is None:
            _logger.warning("Codex-native interrupt skipped for %s: no bridge state.", conv_id)
            return Response(status_code=204)
        if state.session_id != conv_id:
            _logger.warning(
                "Codex-native interrupt skipped for %s: bridge belongs to %s.",
                conv_id,
                state.session_id,
            )
            return Response(status_code=204)
        if state.active_turn_id is None:
            _logger.info("Codex-native interrupt skipped for %s: no active turn.", conv_id)
            return Response(status_code=204)

        codex_client = client_for_transport(
            state.socket_path,
            client_name="omnigent-codex-native-runner",
        )
        try:
            await codex_client.connect()
            await codex_client.request(
                "turn/interrupt",
                {
                    "threadId": state.thread_id,
                    "turnId": state.active_turn_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - surface active-turn interrupt failures to caller.
            _logger.warning(
                "Codex-native turn/interrupt failed for session=%s thread=%s turn=%s",
                conv_id,
                state.thread_id,
                state.active_turn_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native interrupt"),
                },
            )
        finally:
            with contextlib.suppress(Exception):
                await codex_client.close()
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _handle_pi_native_interrupt(conv_id: str) -> Response:
        """
        Stop a pi-native turn by asking the resident Pi extension to abort.

        Pi-native turns live inside the terminal's Pi process. The runner's
        harness task only queues the user's message into the extension inbox
        and returns, so the generic in-process cancel floor has nothing useful
        to cancel. Queue an explicit interrupt payload instead; the extension
        consumes it in the TUI process and calls the active
        ``ExtensionContext.abort()``.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 204 when the interrupt payload was queued; 503 if the
            bridge inbox could not be written.
        """
        from omnigent.pi_native_bridge import bridge_dir_for_session_id, enqueue_interrupt

        try:
            await asyncio.to_thread(
                enqueue_interrupt,
                bridge_dir_for_session_id(conv_id),
            )
        except OSError as exc:
            _logger.warning(
                "Pi-native interrupt failed for session=%s",
                conv_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "pi_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="pi-native interrupt"),
                },
            )
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _teardown_session_terminals(conv_id: str) -> None:
        """Close a session's terminal resources and announce their removal.

        Removes each terminal from the registry and publishes
        ``session.resource.deleted`` so clients drop it immediately (the
        server relay persists it, matching ``sys_terminal_close``).
        Without the events the web UI keeps showing a dead terminal whose
        attach fails with "terminal resource not found". Two callers:

        - claude-native stop: runner-side analog of the CLI launcher's
          ``_close_claude_terminal``, for the host-spawned (web-UI-created)
          path which has no CLI wrapper to observe the killed pane.
        - agent-switch ``reset-state``: the switch closes the old agent's
          terminals while the session stays open, so clients must be told.

        Best-effort — a close failure (e.g. the pane is already dead) must
        not fail the caller.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: None.
        """
        from omnigent.entities.session_resources import terminal_resource_id
        from omnigent.runner.tool_dispatch import _publish_terminal_deleted_event

        terminal_registry = resource_registry.terminal_registry
        if terminal_registry is None:
            return
        # Snapshot (name, key) before closing — close_terminal mutates the
        # registry, so iterating it lazily while closing would skip entries.
        terminals = [
            (entry.terminal_name, entry.session_key)
            for entry in terminal_registry.list_for_conversation(conv_id)
        ]
        for terminal_name, session_key in terminals:
            terminal_id = terminal_resource_id(terminal_name, session_key)
            try:
                await resource_registry.close_terminal(conv_id, terminal_id)
            except (RuntimeError, OSError):
                _logger.warning(
                    "Failed to close terminal %s for session %s during stop",
                    terminal_id,
                    conv_id,
                    exc_info=True,
                )
            _publish_terminal_deleted_event(
                conversation_id=conv_id,
                terminal_name=terminal_name,
                session_key=session_key,
                publish_event=_publish_event,
            )

    async def _handle_claude_native_stop(conv_id: str) -> Response:
        """
        Terminate a claude-native session by killing its tmux session.

        This is the runner-side handler for the Omnigent web UI's "Stop
        session" affordance. Unlike
        :func:`_handle_claude_native_interrupt` (a single ``Escape``
        that cancels the current response but leaves the session
        alive), this kills the tmux session outright, ending the
        ``claude`` process and the pane.

        We do *not* synthesize transcript items the way the interrupt
        handler does: killing the pane causes the wrapper's reconnect
        loop to observe the terminal resource disappear and tear the
        session down through its normal end-of-session path. We do
        publish a ``session.status: idle`` event so the web UI's
        "Working…" spinner clears immediately rather than lingering
        until the wrapper notices the pane is gone — Claude's ``Stop``
        hook never fires on a hard kill.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: 204 on success. 503 if the tmux target is not yet
            advertised (caller treats this as a best-effort failure —
            a missing target means there is no live session to kill).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            kill_session,
        )

        # Resolve the bridge id from the session's labels so
        # ``--resume`` sessions (where bridge_id != conversation_id)
        # land on the right tmux socket. Falls back to ``conv_id`` for
        # legacy single-session bridges; see
        # :func:`_claude_native_bridge_id_for_session`.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: the UI stop must feel snappy; a missing
            # tmux.json means there's nothing left to kill anyway.
            await asyncio.to_thread(kill_session, bridge_dir, timeout_s=1.0)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_stop_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native stop"),
                },
            )
        # The pane is dead; on the host-spawned path no CLI wrapper will
        # observe that and tear the terminal resource down, so do it here
        # — otherwise the web UI keeps showing a live terminal for the
        # stopped session.
        await _teardown_session_terminals(conv_id)
        _publish_event(
            conv_id,
            {"type": "session.status", "status": "idle"},
        )
        # Reclaim the work entry deterministically. If this killed session is a
        # sub-agent worker, mark it cancelled now (and auto-wake its parent)
        # rather than waiting on the wrapper's reconnect loop to notice the dead
        # pane — that lag left the parent thinking the worker was still running.
        # A no-op for a top-level session (it is no one's tracked sub-agent).
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "Claude-native stop succeeded but sub-agent delivery was "
                "not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )
        return Response(status_code=204)

    async def _handle_claude_native_effort_change(
        conv_id: str,
        effort: str | None,
    ) -> Response:
        """
        Type ``/effort <level>`` into Claude's tmux pane.

        Claude-native sessions can't read the persisted
        ``reasoning_effort`` field at turn boundaries — the
        ``--effort`` flag on the ``claude`` binary is baked in at
        spawn. To propagate a live change without restarting the
        pane, this helper types Claude Code's built-in slash
        command into the terminal.

        Skipped silently when:

        * *effort* is ``None`` — Claude Code has no slash form for
          "use the spawn default", so a clear only takes effect on
          the next spawn.
        * *effort* is in ``EFFORT_VALUES`` but not in
          ``CLAUDE_EFFORTS`` (i.e. ``none`` / ``minimal``) —
          injecting ``/effort none`` would type a literal Claude's
          TUI rejects.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param effort: New persisted effort level, e.g. ``"high"``;
            ``None`` when the user cleared the override.
        :returns: 204 on success or skip (caller treats both the
            same — persisted value is the authoritative fallback).
            503 if the tmux target isn't yet advertised (best-
            effort failure).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )
        from omnigent.reasoning_effort import CLAUDE_EFFORTS

        if effort is None or effort not in CLAUDE_EFFORTS:
            # Persistence already happened on the Omnigent server; the
            # next spawn will pick up the new value via ``--effort``.
            return Response(status_code=204)
        # Resolve the bridge id from the session's labels so
        # ``/fork`` sessions (where bridge_id != conv_id) land in
        # the right tmux pane. Falls back to ``conv_id`` for legacy
        # single-session bridges — same pattern
        # ``_handle_claude_native_interrupt`` uses.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        command = f"/effort {effort}"
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached; persisted effort still applies on next spawn.
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command=command,
                timeout_s=1.0,
                auto_confirm=True,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_effort_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="claude-native effort change"
                    ),
                },
            )
        return Response(status_code=204)

    async def _handle_claude_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        """
        Type ``/model <name>`` into Claude's tmux pane.

        Claude-native sessions can't read the persisted ``model_override``
        field at turn boundaries — the ``--model`` flag on the
        ``claude`` binary is baked in at spawn. To propagate a live
        change without restarting the pane, this helper types Claude
        Code's built-in slash command into the terminal.

        Skipped silently when *model* is ``None`` or empty / whitespace
        only — Claude Code has no slash form for "use the spawn
        default", so a clear only takes effect on the next spawn.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param model: New persisted model identifier, e.g.
            ``"claude-opus-4-7"``; ``None`` when the user cleared the
            override.
        :returns: 204 on success or skip (caller treats both the
            same — persisted value is the authoritative fallback).
            503 if the tmux target isn't yet advertised (best-effort
            failure).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )

        if model is None or not model.strip():
            # Persistence already happened on the Omnigent server; the
            # next spawn will pick up the new value via ``--model``.
            return Response(status_code=204)
        # Resolve the bridge id from the session's labels so
        # ``/fork`` sessions (where bridge_id != conv_id) land in
        # the right tmux pane. Falls back to ``conv_id`` for legacy
        # single-session bridges — same pattern
        # ``_handle_claude_native_interrupt`` uses.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        command = f"/model {model.strip()}"
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached; persisted model still applies on next spawn.
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command=command,
                timeout_s=1.0,
                auto_confirm=True,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native model change"),
                },
            )
        return Response(status_code=204)

    async def _handle_claude_native_compact(conv_id: str) -> Response:
        """
        Type ``/compact`` into Claude's tmux pane.

        Explicit compaction on a claude-native session must run inside
        Claude Code, which owns its own context window in the terminal.
        The Omnigent server's own compaction path (``compact_conversation_now``)
        would only summarise the AP-side transcript mirror — it cannot
        shrink Claude's real context and would desync the two. So the
        web-UI ``/compact`` is injected as Claude Code's built-in slash
        command, the same way ``/effort`` and ``/model`` are.

        Returns 200 (not 204) on successful injection so the Omnigent server
        can tell the control was handled in the terminal and skip its
        own AP-side compaction. Other harnesses 204 no-op in the
        ``post_session_events`` dispatch and the Omnigent server runs its
        in-process compaction instead.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 200 once ``/compact`` has been typed into the pane.
            503 if the tmux target isn't yet advertised (the pane is
            not attached, so there is nothing to compact).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )

        # Resolve the bridge id from the session's labels so ``/fork``
        # sessions (where bridge_id != conv_id) land in the right tmux
        # pane. Falls back to ``conv_id`` for legacy single-session
        # bridges — same pattern the effort/model handlers use.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached, so there is no live Claude to compact.
            # ``auto_confirm`` is left False — ``/compact`` does not pop
            # a confirmation dialog the way ``/effort`` / ``/model`` do.
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command="/compact",
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_codex_native_compact(conv_id: str) -> Response:
        """
        Type ``/compact`` into Codex's tmux pane.

        Mirrors :func:`_handle_claude_native_compact` for codex-native
        sessions.  Codex owns its own context window in the terminal,
        so explicit compaction must be injected as the ``/compact``
        slash command — the same rationale as the claude-native path.

        The tmux pane coordinates come from the **resource registry**
        (not a ``tmux.json`` sidecar) because codex-native terminals
        are launched through the registry.  This is the same resolution
        path :func:`_handle_codex_native_cost_popup` uses.

        Returns 200 on successful injection so the Omnigent server
        knows the control was handled in the terminal and skips its
        own AP-side compaction.  204 when no live terminal is
        registered (the server falls back to in-process compaction).

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 200 once ``/compact`` has been typed into the pane.
            204 if no live codex terminal is registered for the session.
            503 if the tmux send-keys invocation fails.
        """
        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            # No live codex terminal — let the server run AP-side compaction.
            return Response(status_code=204)

        socket_path = str(instance.socket_path)
        target = instance.tmux_target

        try:
            await asyncio.to_thread(_inject_codex_compact, socket_path, target)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native compact"),
                },
            )
        return Response(status_code=200)

    def _inject_codex_compact(socket_path: str, target: str) -> None:
        """
        Blocking helper: type ``/compact`` into a codex tmux pane.

        Uses the same ``C-u`` → literal ``/compact`` → ``Enter``
        sequence that :func:`~omnigent.claude_native_bridge.inject_slash_command`
        uses for claude-native.  Factored into its own function so
        :func:`_handle_codex_native_compact` can run it via
        ``asyncio.to_thread`` without importing at call time.

        :param socket_path: Absolute path to the tmux socket, e.g.
            ``"/tmp/.../codex-main.sock"``.
        :param target: Tmux target pane, e.g. ``"main"``.
        :raises RuntimeError: If any ``tmux send-keys`` invocation fails.
        """
        from omnigent.claude_native_bridge import _run_tmux

        # Clear any draft the user is mid-typing.
        _run_tmux(socket_path, "send-keys", "-t", target, "C-u")
        # Paste ``/compact`` literally.
        _run_tmux(socket_path, "send-keys", "-l", "-t", target, "/compact")
        # Submit.
        _run_tmux(socket_path, "send-keys", "-t", target, "Enter")

    async def _handle_claude_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        """
        Overlay a cost-budget approval modal on Claude's tmux pane.

        A server-side tool-policy ASK (the ``TOOL_CALL`` gate, e.g. a
        cost-budget warning checkpoint) parks and is published to the
        web UI as an ``ApprovalCard``. For a user driving the session in the native
        terminal — who never sees the web card — the Omnigent server forwards a
        ``cost_approval_popup`` control event here, and this handler pops
        a ``tmux display-popup`` modal in the pane. The popup resolves the
        **same** elicitation via the same endpoint the web card uses, so
        whichever surface answers first wins and the other clears. The
        server-side approval Future (and its decline-on-timeout → stop
        behaviour) is unchanged — this only adds a second answer surface.

        Best-effort: the modal is fired detached (it does not block this
        handler), and a pane that isn't attached / a tmux too old for
        ``display-popup`` simply leaves the web card as the only surface.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param elicitation_id: Outstanding elicitation correlation id,
            e.g. ``"elicit_deadbeef"``.
        :param message: Approval reason to display, e.g.
            ``"Session cost $0.12 crossed the $0.10 checkpoint. Continue?"``.
        :param policy_name: Name of the deciding policy, rendered as the
            modal header. ``None`` falls back to a generic header.
        :returns: 204 once the popup has been dispatched (or skipped when
            the pane isn't advertised). 503 only if resolving the bridge
            target raised — a best-effort failure the web card covers.
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            display_cost_approval_popup,
        )

        # Resolve the bridge id from the session's labels so ``/fork``
        # sessions (where bridge_id != conv_id) land in the right tmux
        # pane. Falls back to ``conv_id`` for legacy single-session
        # bridges — same pattern the effort/model/compact handlers use.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached, so there is no client to render the modal — the
            # web ApprovalCard is the only surface and that is fine.
            await asyncio.to_thread(
                display_cost_approval_popup,
                bridge_dir,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_cost_popup_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native cost popup"),
                },
            )
        return Response(status_code=204)

    async def _handle_codex_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        """
        Overlay a cost-budget approval modal on Codex's tmux pane.

        The codex-native counterpart of
        :func:`_handle_claude_native_cost_popup`. Codex does not advertise
        a ``tmux.json`` (its terminal is launched through the resource
        registry), so the pane's socket/target come from the registry
        instance — the same source the web-terminal attach uses — and AP
        routing comes from this bridge's ``policy_hook.json``. Resolution
        differs; the actual popup launch is the shared, harness-agnostic
        :func:`omnigent.native_cost_popup.launch_cost_popup`.

        Best-effort: skips (204) when no live codex terminal is registered
        for the session, so the web ApprovalCard remains the surface.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param elicitation_id: Outstanding elicitation correlation id,
            e.g. ``"elicit_deadbeef"``.
        :param message: Approval reason to display.
        :param policy_name: Name of the deciding policy, rendered as the
            modal header. ``None`` falls back to a generic header.
        :returns: 204 once the popup is dispatched (or skipped when no
            terminal is registered). 503 if launching raised.
        """
        from omnigent.codex_native_bridge import _POLICY_HOOK_FILE, bridge_dir_for_bridge_id
        from omnigent.native_cost_popup import launch_cost_popup

        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            # No live codex terminal to render on; web card is the surface.
            return Response(status_code=204)
        config_file = bridge_dir_for_bridge_id(conv_id) / _POLICY_HOOK_FILE
        try:
            await asyncio.to_thread(
                launch_cost_popup,
                str(instance.socket_path),
                instance.tmux_target,
                config_file,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_cost_popup_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native cost popup"),
                },
            )
        return Response(status_code=204)

    async def _native_cost_popup_config_file(conv_id: str, harness: str) -> Path:
        """
        Resolve the AP-routing config file the cost popup reads, per harness.

        The popup script reads ``ap_server_url`` + ``ap_auth_headers`` from
        this file: ``permission_hook.json`` in the claude-native bridge dir,
        ``policy_hook.json`` in the codex-native bridge dir.

        :param conv_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :param harness: ``"claude-native"`` or ``"codex-native"``.
        :returns: Path to the harness's AP-routing config file.
        """
        if harness == "claude-native":
            from omnigent import claude_native_bridge as _cnb

            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client, session_id=conv_id
            )
            return _cnb.bridge_dir_for_bridge_id(bridge_id) / _cnb._PERMISSION_HOOK_FILE
        from omnigent import codex_native_bridge as _cxb

        return _cxb.bridge_dir_for_bridge_id(conv_id) / _cxb._POLICY_HOOK_FILE

    async def _repop_pending_cost_popup_on_attach(
        conv_id: str,
        socket_path: str,
        tmux_target: str,
    ) -> None:
        """
        Re-pop a still-pending native approval on a newly attached client.

        Covers the case where the ASK fired while no terminal client was
        attached (the user was in the web Chat), then the user opens the
        Terminal: on attach this re-checks the session snapshot and, if a
        native approval is still outstanding — the server-side policy gate
        (``TOOL_CALL`` / ``LLM_REQUEST``, e.g. a cost-budget checkpoint, or
        the ``REQUEST`` gate a native session enforces via the
        ``UserPromptSubmit`` hook) — pops it on the now-attached client.
        Self-correcting — it only pops while the elicitation is still
        pending, so an already-answered approval is not re-shown. Complements
        the ASK-time forward (which covers clients attached *before* the
        ASK). Best-effort: any miss leaves the web card.

        :param conv_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :param socket_path: tmux socket of the attaching pane.
        :param tmux_target: tmux target of the attaching pane, e.g. ``"main"``.
        :returns: None.
        """
        harness = _session_harness_name(conv_id)
        if harness not in ("claude-native", "codex-native"):
            return
        from omnigent.native_cost_popup import launch_cost_popup, wait_for_tmux_client

        # The attach is in flight when this task starts; wait for the client
        # to register so there is something to render the modal on.
        attached = await asyncio.to_thread(
            wait_for_tmux_client, socket_path, tmux_target, timeout_s=5.0
        )
        if not attached:
            return
        try:
            resp = await server_client.get(f"/v1/sessions/{conv_id}", timeout=10.0)
        except httpx.HTTPError:
            return
        if resp.status_code != 200:
            return
        pending = resp.json().get("pending_elicitations") or []
        # The native popup surfaces the server-side policy gate, which parks
        # and resolves via the same endpoint. Re-pop whichever is pending:
        # the tool-policy gate (tool_call / llm_request — including
        # cost-budget checkpoints) and the request-phase gate (request),
        # which native sessions enforce via the UserPromptSubmit hook. A
        # request-phase ASK typically fires while the user is in the web
        # Chat (no client attached), so the on-attach re-pop is its main
        # path onto the terminal.
        approval = next(
            (
                e
                for e in pending
                if isinstance(e, dict)
                and isinstance(e.get("params"), dict)
                and e["params"].get("phase") in ("request", "tool_call", "llm_request")
            ),
            None,
        )
        if approval is None:
            return
        elicitation_id = approval.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            return
        message = approval["params"].get("message") or "Approval required"
        policy_name = approval["params"].get("policy_name")
        config_file = await _native_cost_popup_config_file(conv_id, harness)
        await asyncio.to_thread(
            launch_cost_popup,
            socket_path,
            tmux_target,
            config_file,
            session_id=conv_id,
            elicitation_id=elicitation_id,
            message=message,
            policy_name=policy_name if isinstance(policy_name, str) and policy_name else None,
        )

    def _on_proxy_stream_end(
        conv_id: str,
        *,
        error: dict[str, Any] | None = None,
    ) -> None:
        """
        Turn-end bookkeeping called from proxy_stream completion points.

        Removes the session from ``_active_turns``, publishes the
        appropriate ``session.status`` event (``idle`` on success
        or cancellation, ``failed`` on error), and schedules a
        post-turn buffer check.

        For a scaffold (in-process) sub-agent, a *successful* turn end is
        reported to the parent as the terminal completion only when no
        continuation is buffered — otherwise the intermediate turn's text
        would be delivered and the real final synthesis dropped (the
        already-terminal entry short-circuits later delivery). Deferring to
        the continuation's own empty-buffer stream end can't strand the
        result: every ``_run_turn_bg`` exit routes back through here, and
        ``_check_and_start_next_turn`` always starts a turn while the buffer
        is non-empty. The error/interrupt/cancel branches stay unconditional
        — those are genuine terminal outcomes, not intermediate narration.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param error: If the turn ended due to an error, a dict
            with at least a ``"message"`` key. ``None`` for
            successful completion.
        """

        _active_turns.pop(conv_id, None)
        # Skip the idle transient when a buffered message will start a
        # continuation turn immediately — `_check_and_start_next_turn`
        # publishes "running" microseconds later, and the in-between idle
        # otherwise hides the Working indicator on the client.
        # `failed` is always published so a real error is never swallowed.
        has_buffered = bool(_session_message_buffers.get(conv_id))
        was_interrupted = conv_id in _interrupted_sessions
        if was_interrupted:
            _interrupted_sessions.discard(conv_id)
            _append_cancellation_items(conv_id)
            if not has_buffered:
                _publish_turn_status(conv_id, "idle")
        elif error is not None:
            # Carry the failure detail so a SETUP-phase failure (no
            # response.failed event) still surfaces a real error message to
            # clients instead of ending silently. ``failed`` is published
            # for every harness (including claude-native) — see
            # _publish_turn_status.
            _publish_turn_status(conv_id, "failed", error=_normalize_turn_error(error))
        else:
            if not has_buffered:
                _publish_turn_status(conv_id, "idle")
        if was_interrupted:
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="cancelled",
                output="[System: sub-agent interrupted]",
            )
        elif error is not None:
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="failed",
                output=f"Error: sub-agent turn failed: {error.get('message', 'unknown')}",
            )
        elif not _is_native_harness(conv_id) and not has_buffered:
            # Defer the success delivery while a continuation is buffered —
            # see the docstring. The continuation turn's own empty-buffer
            # stream end delivers exactly once with the final assistant text.
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="completed",
                output=_extract_last_assistant_text(conv_id),
            )
        # Belt-and-suspenders: POST the terminal status directly to the
        try:
            loop = asyncio.get_running_loop()
            _cont = loop.create_task(
                _check_and_start_next_turn(conv_id),
            )
            _cont.add_done_callback(_background_tasks.discard)
            _background_tasks.add(_cont)
        except RuntimeError:
            pass

    async def _cancel_active_turn(
        conv_id: str, expected_task: asyncio.Task[None] | None = None
    ) -> bool:
        """Force-cancel a session's in-flight turn task — the cancel floor.

        The scaffold's interrupt only takes effect when the executor adapter
        polls between emitted events, so a turn blocked mid-op — or one whose
        executor has no native interrupt — can hang until natural completion.
        Cancelling the runner turn task (the proven primitive from
        :func:`delete_session`) unwinds the runner side regardless of harness.

        On a cancel during the streaming phase, ``_drain_streaming_response``'s
        ``CancelledError`` handler pops ``_active_turns`` and publishes ``idle``
        — but it does NOT append the cancellation items (synthetic outputs for
        dangling tool calls + the interrupted marker). So when the session was
        interrupted, append them here. The ``_interrupted_sessions`` discard is
        the idempotency token: a natural completion that races the cancel runs
        ``_on_proxy_stream_end``, which discards the flag first, so this block
        then no-ops.

        A cancel during the *setup* phase (before ``_drain_streaming_response``
        is entered) raises ``CancelledError`` — a ``BaseException`` — past
        ``_run_turn_bg``'s ``except Exception``, so neither handler runs and
        ``_active_turns`` is left stale (every later message then buffers and
        the session hangs). Detected by the entry still pointing at this task
        after the await; we run the full terminal bookkeeping via
        ``_on_proxy_stream_end`` to recover.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param expected_task: If given, only cancel when this exact task is
            still the live turn. Guards against cancelling a continuation turn
            that replaced the original (the original completed naturally while
            the caller was forwarding the interrupt) — killing that would orphan
            its dangling tool calls.
        :returns: ``True`` if a running turn was cancelled, ``False`` if there
            was no live turn task (or it was replaced by a continuation).
        """
        turn_task = _active_turns.get(conv_id)
        if not isinstance(turn_task, asyncio.Task) or turn_task.done():
            return False
        if expected_task is not None and turn_task is not expected_task:
            return False
        turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn_task
        if _active_turns.get(conv_id) is turn_task:
            # Setup-phase cancel: no handler cleaned up. _on_proxy_stream_end
            # pops _active_turns, publishes idle (or starts a buffered
            # continuation), and runs the interrupted path (flag-discard +
            # cancellation items) itself, so skip the block below.
            _on_proxy_stream_end(conv_id)
            return True
        if conv_id in _interrupted_sessions:
            _interrupted_sessions.discard(conv_id)
            _append_cancellation_items(conv_id)
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="cancelled",
                output="[System: sub-agent interrupted]",
            )
        return True

    async def _cancel_inprocess_turn(conv_id: str) -> None:
        """Stop an in-process (non-native) harness's in-flight turn.

        Shared by the ``interrupt`` and ``stop_session`` dispatch. No-ops when no
        turn is in flight (a stale interrupted flag would taint the next turn).
        Forward the interrupt to the harness FIRST — while its turn is still
        in-flight — so the harness's interrupt handler engages (cancels the turn
        and drops the claude-sdk session); THEN force-cancel the runner turn task
        as the floor. Order matters: cancelling first closes the runner's harness
        stream, which ends the harness turn, so the later interrupt 404s and the
        session is never dropped — the next message then resumes the abandoned
        turn and the agent runs one message behind.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        """
        target = _active_turns.get(conv_id)
        if not isinstance(target, asyncio.Task) or target.done():
            return
        _interrupted_sessions.add(conv_id)
        try:
            harness_client = await process_manager.get_client(conv_id, "any")
            await harness_client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt"},
                # Bounded under the Omnigent server's 5s stop deadline.
                timeout=3.0,
            )
        except Exception:  # noqa: BLE001 — best-effort: harness may have exited
            _logger.warning(
                "Interrupt forward to harness failed for %s",
                conv_id,
                exc_info=True,
            )
        await _cancel_active_turn(conv_id, expected_task=target)

    async def _check_and_start_next_turn(
        session_id: str,
    ) -> None:
        """
        Drain the message buffer and start a continuation turn.

        Called after a turn ends. If messages were buffered while
        the turn was active, pops the first one and starts a new
        background turn. The background turn's completion will
        recursively call this function.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """

        buf = _session_message_buffers.get(session_id)
        if not buf:
            # Parent going idle: clear any wake-debounce flag left stuck by a
            # mid-turn-consumed injection (re-arming if results are stranded),
            # so the next sub-agent completion can wake the parent.
            _rewake_parent_if_inbox_stranded(session_id)
            return

        if _is_native_harness(session_id):
            # Native harnesses type only the latest user message per turn,
            # so collapsing the buffer to its last entry would drop every
            # earlier message from the terminal. Drain ONE message at a
            # time, in order: this turn delivers ``next_body``, and its
            # completion re-enters here for the next buffered message.
            # No batching — each typed exactly once (RUNNER_MESSAGE_INGEST.md
            # Part C).
            next_body = buf.pop(0)
            if not buf:
                _session_message_buffers.pop(session_id, None)
            _session_histories.setdefault(session_id, []).append(
                {
                    "type": "message",
                    "role": next_body.get("role", "user"),
                    "content": next_body.get("content", []),
                }
            )
        else:
            # LLM harnesses: drain ALL buffered messages into history so
            # rapid-fire user input ("hi", "can", "you", "fix", "bugs")
            # becomes a single continuation turn instead of one turn per
            # word. The harness sees every message via history; the turn
            # responds once.
            all_bodies = list(buf)
            buf.clear()
            _session_message_buffers.pop(session_id, None)

            for body in all_bodies:
                _session_histories.setdefault(session_id, []).append(
                    {
                        "type": "message",
                        "role": body.get("role", "user"),
                        "content": body.get("content", []),
                    }
                )
            next_body = all_bodies[-1]

        # Register the continuation turn BEFORE the await so a
        # concurrent POST sees an active turn (invariant I2).
        _active_turns[session_id] = None

        _publish_turn_status(session_id, "running")

        # Use _run_turn_bg so the continuation turn gets full
        # history, tool schemas, instructions — identical to a
        # first turn. Without this, the harness only sees the
        # raw buffered message with no prior context.
        _turn_task = asyncio.create_task(
            _run_turn_bg(next_body, session_id),
            name=f"turn-cont-{session_id}",
        )
        _active_turns[session_id] = _turn_task
        _turn_task.add_done_callback(
            _background_tasks.discard,
        )
        _background_tasks.add(_turn_task)

    async def _post_subagent_wake_notice(parent_id: str, notice: str, child_id: str) -> None:
        """
        POST a framework wake notice to a parent session's event stream.

        Mirrors the timer-firing POST in ``tool_dispatch._timer_loop``: the
        synthetic ``user`` message rides the normal ingest path, which starts
        a continuation turn when the parent is idle or buffers (coalescing
        with any other pending messages into a single later turn) when a turn
        is already active. The completion payload itself already sits in the
        parent inbox; this only delivers the wake signal.

        Delivery is delegated to :func:`_deliver_subagent_wake_post`, which
        checks the response status and retries transient failures (e.g. a
        503 ``RUNNER_UNAVAILABLE`` while the parent's runner tunnel
        reconnects). On terminal failure the debounce flag is released so a
        later completion can retry — no parent turn will run to clear it
        otherwise — and a warning is logged.

        :param parent_id: Parent session to wake, e.g. ``"conv_parent123"``.
        :param notice: The ``[System: ...]`` notice text to inject.
        :param child_id: Completing child session id, included only for log
            context, e.g. ``"conv_child456"``.
        :returns: None.
        """
        delivered = await _deliver_subagent_wake_post(server_client, parent_id, notice)
        if not delivered:
            # A failed wake must not crash turn-end; the inbox keeps the result.
            # Release the debounce flag so a later completion can retry the
            # wake — no parent turn will run to clear it otherwise.
            _subagent_wake_pending.discard(parent_id)
            _logger.warning(
                "Sub-agent wake POST failed for parent=%s child=%s after %d attempt(s); "
                "result remains in the parent inbox until the next wake",
                parent_id,
                child_id,
                _WAKE_POST_MAX_ATTEMPTS,
            )

    def _schedule_subagent_wake(entry: _SubagentWorkEntry) -> None:
        """
        Schedule a wake POST after a child completion lands in the parent inbox.

        Called by ``_mark_subagent_terminal_and_wake`` once per delivery (it
        gates on the not-delivered → delivered transition), and a parent is
        never its own child, so a parent's own turn-end never re-wakes it.

        Debounced per parent: while a wake is outstanding (posted, not yet
        consumed by the parent's next turn start), further completions skip
        posting — a fan-out's results all queue in the one inbox, which a
        single wake turn drains via ``sys_read_inbox``. This prevents the
        wake storm (one /events message per completion) that churns turns and
        trips the executor's per-turn tool-context guard.

        :param entry: The just-delivered terminal sub-agent work entry.
        :returns: None.
        """
        # A session is never its own sub-agent; never wake on self.
        if entry.parent_session_id == entry.child_session_id:
            return
        inbox = _session_inboxes.get(entry.parent_session_id)
        if inbox is None:
            return
        # Debounce: one outstanding wake per parent (cleared at turn start).
        if entry.parent_session_id in _subagent_wake_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Off the event loop (defensive); completion drains on the next turn.
            return
        _subagent_wake_pending.add(entry.parent_session_id)
        # qsize counts the item just delivered by put_nowait (>= 1).
        notice = _format_subagent_wake_notice(
            agent=entry.agent,
            title=entry.title,
            status=entry.status,
            pending=inbox.qsize(),
        )
        _wake_task = loop.create_task(
            _post_subagent_wake_notice(entry.parent_session_id, notice, entry.child_session_id)
        )
        _wake_task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_wake_task)

    def _rewake_parent_if_inbox_stranded(parent_session_id: str) -> None:
        """
        Clear a stuck wake flag on parent idle, re-arming if results remain.

        The wake debounce (``_subagent_wake_pending``) is cleared only at turn
        start. A wake consumed as a mid-turn injection never enters
        ``_run_turn_bg``, so the flag stays stuck with no future turn to clear
        it — and the next completion is then debounced and stranded. This runs
        when the parent idles (turn ended, no buffered continuation), so the
        flag is always released here regardless of inbox state; otherwise a
        wake the parent already drained in that same turn would leave the flag
        set and strand the *next* completion. The recovery wake is only posted
        when the inbox still holds undrained results. (The fan-out coalesce
        path is unaffected: it has no turn here, so this is never reached and
        its single outstanding wake still starts the draining turn.)

        :param parent_session_id: Parent whose turn just ended, e.g.
            ``"conv_parent123"``.
        :returns: None.
        """
        if parent_session_id not in _subagent_wake_pending:
            return
        # Always drop the stale flag: the turn just ended with no continuation,
        # so nothing else will clear it. Leaving it set (even on an emptied
        # inbox) would debounce and strand the next completion.
        _subagent_wake_pending.discard(parent_session_id)
        inbox = _session_inboxes.get(parent_session_id)
        if inbox is None or inbox.empty():
            # Flag cleared; nothing stranded to re-wake on.
            return
        entries = list_subagent_work(parent_session_id)
        if not entries:
            return
        # Use the latest completed child so the notice names a real (agent,
        # title); _schedule_subagent_wake recomputes the count from the inbox.
        latest = max(
            entries,
            key=lambda entry: entry.completed_at if entry.completed_at is not None else 0.0,
        )
        _schedule_subagent_wake(latest)

    def _mark_subagent_terminal_and_wake(
        child_session_id: str, *, status: str, output: str | None
    ) -> _SubagentDeliveryAck:
        """
        Mark a child terminal and wake its parent if a payload was delivered.

        Thin wrapper over ``mark_subagent_work_terminal`` for the turn-end
        call sites: it wakes the parent only on a genuine not-delivered →
        delivered transition, so a re-marked (already-terminal) child or an
        untracked session (e.g. the orchestrator's own turn ending) never
        fires a spurious or looping wake.

        :param child_session_id: Child session id, e.g. ``"conv_child456"``.
        :param status: Terminal status: ``"completed"``, ``"failed"``, or
            ``"cancelled"``.
        :param output: Child output or error text. ``None`` means the
            completion had no assistant text to deliver.
        :returns: Delivery acknowledgement for the terminal report.
        """
        ack = mark_subagent_work_terminal(child_session_id, status=status, output=output)
        if ack.entry is not None and ack.delivered_now:
            _schedule_subagent_wake(ack.entry)
        return ack

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

    async def _run_turn_advisor(
        msg_body: dict[str, Any],
        conv: str,
        spec: Any,  # type: ignore[explicit-any]  # resolved AgentSpec or None
    ) -> AdvisorTurnResult | None:
        """
        Run the cost advisor for one turn (no-op unless the spec opts in
        via ``executor.config.cost_optimize``).

        Every turn path that reaches the harness must run this so the
        per-turn brain-model verdict is judged, recorded, and (optimize
        mode, claude-sdk) applied to this turn's harness request.

        :param msg_body: The forwarded message body; the turn's query is
            read from ``msg_body["content"]`` and the user model pin from
            ``msg_body["model_override"]``.
        :param conv: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param spec: The resolved agent spec for the session, or ``None``
            (advisor skipped).
        :returns: The verdict + apply_model + note, or ``None`` when the
            turn runs unadvised.
        """
        from datetime import datetime, timezone

        from omnigent.runner.cost_advisor import maybe_run_advisor

        # Resolve the brain harness so the advisor can scope application
        # (claude-sdk only). Mirrors _resolve_harness_config's derivation.
        harness: str | None = None
        if spec is not None:
            _h = spec.executor.config.get("harness") or spec.executor.type
            harness = canonicalize_harness(_h) or _h

        # Per-session Cost Optimized toggle, read defensively
        # off the snapshot so this still works against servers without
        # the column. Precedence (override > spec mode) is resolved inside.
        cost_control_mode_override = await _fetch_cost_control_mode_override(server_client, conv)
        return await maybe_run_advisor(
            spec=spec,
            conversation_id=conv,
            turn_content=msg_body.get("content") or [],
            server_client=server_client,
            turn_anchor=datetime.now(timezone.utc).isoformat(),
            harness=harness,
            # The server-forwarded session model pin (/model or web picker).
            # When set it BEATS the advisor (verdict recorded, not applied).
            user_model_override=msg_body.get("model_override"),
            cost_control_mode_override=cost_control_mode_override,
        )

    def _apply_advisor_for_turn(
        body: dict[str, Any],
        conv: str,
        result: AdvisorTurnResult | None,
        user_model_override: str | None = None,
    ) -> None:
        """
        Apply an advisor result to the turn body and keep the brain sticky.

        Optimize mode applied a model this turn: stamp it on the body and
        remember it. A turn that applied NOTHING (advise mode, a
        conversational/failed judge, or advisor off) carries forward the
        last applied model — so the claude-sdk brain stays on the advisor's
        last selection across conversational turns instead of flapping back
        to the gateway/spec default (whose ``set_model(None)`` would reset
        it).

        An explicit USER pin disables the carry-forward entirely. The pin
        reaches the harness via the spawn env (``HARNESS_<H>_MODEL``), which
        the body's ``model_override`` (→ ``cfg.model``) would BEAT in the
        executor — so stamping the sticky model here would silently override
        the user's choice (the live ``/model``-vs-advisor precedence bug).
        The stored selection is also dropped: user intent supersedes the
        advisor's last applied model, and resurrecting it after an unpin
        would flap the brain to a stale choice.

        :param body: The harness request body, mutated in place (caller owns
            it — copy-on-write at the streaming site).
        :param conv: Session id, key into the sticky-model state.
        :param result: The advisor turn result, or ``None`` (no verdict).
        :param user_model_override: The session's user model pin from the
            inbound message body, e.g. ``"databricks-claude-sonnet-4-6"``,
            or ``None``. When set, no advisor model is stamped this turn.
        """
        if user_model_override:
            _session_advisor_applied_model.pop(conv, None)
            return
        if result is not None and result.apply_model is not None:
            _apply_advisor_to_body(body, result)
            _session_advisor_applied_model[conv] = result.apply_model
            return
        # No application this turn: keep the brain on the last applied model
        # (if any). The body's own model_override (already advisor-free on
        # this path) still wins if a caller set one.
        sticky = _session_advisor_applied_model.get(conv)
        if sticky is not None and not body.get("model_override"):
            body["model_override"] = sticky

    async def _advisor_spec_for_session(conv: str) -> Any:  # type: ignore[explicit-any]  # resolved AgentSpec or None
        """
        Best-effort spec resolution for the ``stream=true`` advisor run.

        Applies the sub-agent override so a child session plans against
        its own spec, not the parent orchestrator's; resolution failures
        return ``None`` (turn runs unadvised) rather than failing a turn
        for a feature that is dark by default.

        :param conv: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The resolved spec, or ``None``.
        """
        try:
            spec = _unwrap_resolved_spec(await _resolve_session_spec_entry(conv))
        except (OmnigentError, httpx.HTTPError, RuntimeError):
            _logger.warning(
                "cost_advisor: spec resolution failed for %s; turn runs unadvised",
                conv,
                exc_info=True,
            )
            return None
        _sa_name = _session_sub_agent_names.get(conv)
        if _sa_name and spec is not None:
            from omnigent.runtime.workflow import _find_spec_by_name

            sub_spec = _find_spec_by_name(spec, _sa_name)
            if sub_spec is not None:
                spec = sub_spec
        return spec

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

