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


