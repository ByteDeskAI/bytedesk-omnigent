    @app.post("/v1/elicitations/{elicitation_id}")
    async def elicitation(elicitation_id: str, request: Request) -> JSONResponse:
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={"error": "not_implemented", "detail": "Runner not configured"},
            )
        body = await request.json()
        # The server includes response_id when relaying elicitations
        # to the runner. Resolve conversation from it.
        response_id = body.get("response_id")
        if not response_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "response_id required in elicitation body",
                },
            )
        conv_id = await _resolve_conversation_id(response_id)
        if conv_id is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "detail": f"Cannot resolve response {response_id}"},
            )
        try:
            client = await process_manager.get_client(conv_id, "any")
            # Translate the MCP-shape ElicitationResult body
            # ({"action": ..., "content": ...}) onto the harness's
            # discriminated ``approval`` event per
            # ``designs/session_rearchitecture.md`` §3.
            event_body = {
                "type": "approval",
                "elicitation_id": elicitation_id,
                "action": body.get("action"),
            }
            if body.get("content") is not None:
                event_body["content"] = body["content"]
            resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json=event_body,
                timeout=30.0,
            )
            return _forward_harness_response(resp)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={
                    "error": "elicitation_failed",
                    "detail": _client_safe_error_detail(exc, context="elicitation forward"),
                },
            )
