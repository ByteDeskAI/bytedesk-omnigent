    @app.get("/v1/sessions/{session_id}/stream")
    async def stream_session(session_id: str) -> StreamingResponse:
        """
        Subscribe to live SSE events for a session.

        Reads from the per-session event queue. Events
        accumulate in the queue while no subscriber is
        connected, so tunnel drops don't lose events — the
        relay drains on reconnect. Events are removed from
        the queue after reading.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: Long-lived ``text/event-stream`` response.
        """

        async def _event_generator() -> AsyncIterator[bytes]:
            """
            Yield SSE frames from the per-session event queue.

            Blocks on ``queue.get()`` with a heartbeat timeout so
            between-turn idle periods emit keepalive bytes. Without
            these, an intermediate proxy can drop the long-lived
            HTTP connection, leaving the Omnigent relay on a half-open
            socket that blocks forever. Lazily creates the queue if
            the relay connects before session creation (the REPL's
            SSE subscription races the session POST).

            :returns: Async iterator of UTF-8 encoded SSE frames.
            """
            queue = _session_event_queues.get(session_id)
            if queue is None:
                queue = asyncio.Queue()
                _session_event_queues[session_id] = queue
            heartbeat_frame = b'data: {"type": "session.heartbeat"}\n\n'
            # Immediate ready ack: Omnigent waits for this frame before
            # forwarding no-replay user input, proving its relay has
            # reached the runner stream and created/attached to the
            # per-session queue. Later heartbeats are idle keepalives.
            yield heartbeat_frame
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_SESSION_STREAM_HEARTBEAT_S
                    )
                except asyncio.TimeoutError:
                    yield heartbeat_frame
                    continue
                if event is None:
                    break
                frame = "data: " + json.dumps(event) + "\n\n"
                try:
                    yield frame.encode("utf-8")
                except (GeneratorExit, asyncio.CancelledError):
                    queue.put_nowait(event)
                    return
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

