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


