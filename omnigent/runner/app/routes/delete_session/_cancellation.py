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


