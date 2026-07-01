    @app.post("/v1/summarize")
    async def summarize(request: Request) -> JSONResponse:
        """Summarize a message list using the runner's LLM credentials.

        Accepts a JSON body with ``messages``, ``model``, an optional
        ``connection`` dict, and an optional ``profile`` string.  For
        Databricks models, ``profile`` is used to resolve fresh OAuth
        credentials from the runner's own ``~/.databrickscfg`` — so
        the runner's credentials are used, not the Omnigent server's static
        token.

        :param request: FastAPI request carrying the JSON body.
        :returns: JSON with ``"text"`` (summary string) and
            ``"token_count"`` (tiktoken estimate) keys.
        """
        body = await request.json()
        messages = body.get("messages")
        model = body.get("model")
        if not isinstance(messages, list) or not model:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'messages' (list) and 'model' (str) are required",
                    }
                },
            )
        # Resolve LLM connection for the summarization call. Precedence:
        # 1. Explicit connection in the payload (non-Databricks callers).
        # 2. Spec auth from the session's cached spec (DatabricksAuth
        #    profile or ApiKeyAuth).
        # 3. Ambient env-var auth (DATABRICKS_CONFIG_PROFILE / DEFAULT).
        connection: dict[str, str] | None = body.get("connection") or None
        if connection is None:
            session_id: str | None = body.get("session_id")
            if session_id is not None:
                connection = _resolve_summarize_connection(
                    session_id,
                    model,
                )
        llm_client = _get_runner_llm_client()
        resp = await llm_client.responses.create(
            model=model,
            input=build_summarization_input(messages),
            instructions=build_summarization_prompt(messages),
            tools=[],
            connection_params=connection,
        )
        summary_text = extract_summary_text(resp)
        import tiktoken

        bare = model.split("/", 1)[-1] if "/" in model else model
        try:
            enc = tiktoken.encoding_for_model(bare)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        token_count = len(enc.encode(summary_text))
        return JSONResponse(content={"text": summary_text, "token_count": token_count})

