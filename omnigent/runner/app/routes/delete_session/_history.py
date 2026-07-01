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


