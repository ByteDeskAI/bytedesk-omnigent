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

