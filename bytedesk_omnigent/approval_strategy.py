"""The ``ApprovalStrategy`` seam for the ASK / elicitation flow (BDP-2341, ADR-0008/0142).

The core ASK round-trip (:func:`omnigent.runtime.policies.approval._await_elicitation`)
hardcodes two responsibilities into one function:

1. **compose the ask** — turn an engine-composed ASK :class:`PolicyResult` into the
   wire-shaped :class:`~omnigent.policies.types.ElicitationRequest`, register the
   pending elicitation row, and emit the ``response.elicitation_request`` SSE event.
2. **apply the verdict** — strictly parse the consumer's MCP ``ElicitResult`` body
   (only ``action == "accept"`` approves) and, *only on approve*, apply the
   ASK-accumulated ``set_labels`` + ``state_updates`` through the engine
   (POLICIES.md §7.2: a denied / cancelled / timed-out ASK leaves no side effects).

This module factors those two responsibilities behind a Strategy (ADR-0008) — a
:class:`ApprovalStrategy` protocol + a deploy-time registry + a
:class:`DefaultApprovalStrategy` that reproduces today's hardcoded behavior
**verbatim** by delegating to the existing core helpers. It is purely additive: it
introduces the seam without changing any current behavior. The live alternate
strategy (e.g. a ByteDesk-platform approval that routes through Office / SignalR
instead of the in-process SSE elicitation) is registered by the server at deploy;
until one is registered, callers fall back to :class:`DefaultApprovalStrategy`,
which is byte-for-byte the established path.

The three I/O seams (``register``, ``emit``, ``park``) and the
:class:`~omnigent.runtime.policies.engine.PolicyEngine` are passed in per call —
mirroring :func:`_await_elicitation` — so a strategy stays unit-provable without a
live task_store + SSE stack.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from omnigent.policies.types import ElicitationRequest, PolicyResult
from omnigent.runtime.policies.approval import (
    _parse_verdict,
    _truncate,
    build_elicitation_params_json,
    build_elicitation_request_event,
    resolve_ask_timeout,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import Phase

_logger = logging.getLogger(__name__)

# Seam callback contracts — identical shapes to ``_await_elicitation``'s
# parameters. The workflow binds real implementations (task_store register,
# ``_write_output`` SSE emit, ``tool_result``-topic park) at wiring time; tests
# inject canned recorders / awaitables.
_RegisterCallback = Callable[[str, str, str], None]
_EmitCallback = Callable[[dict[str, Any]], None]
_ParkCallback = Callable[[str, int], Awaitable[str | None]]


@runtime_checkable
class ApprovalStrategy(Protocol):
    """Composes the ASK and applies its verdict — the elicitation seam.

    Two responsibilities, split out of the hardcoded
    :func:`_await_elicitation`:

    - :meth:`compose_ask` builds the :class:`ElicitationRequest`, registers the
      pending row, and emits the elicitation event (returning the ``elicitation_id``
      so the caller can correlate the park).
    - :meth:`apply_verdict` strictly parses the raw verdict and, only on approve,
      applies the ASK-accumulated ``set_labels`` + ``state_updates`` through the
      engine (POLICIES.md §7.2 — a denied ASK leaves no trace).

    Implementations must preserve the fail-closed contract: anything other than an
    exact ``action == "accept"`` verdict is a refusal, and a refusal applies nothing.
    """

    def compose_ask(
        self,
        *,
        elicitation_id: str,
        task_id: str,
        result: PolicyResult,
        phase: Phase,
        content_preview: str,
        register: _RegisterCallback,
        emit: _EmitCallback,
    ) -> ElicitationRequest:
        """Build, register, and emit the elicitation for ``result``.

        :param elicitation_id: Pre-minted id correlating this request to the
            consumer's reply (the caller mints it so it can park on it).
        :param task_id: The sub-agent's task id (the parked workflow) recorded on
            the pending elicitation row.
        :param result: Engine-composed ASK :class:`PolicyResult` — carries the
            combined ``reason``, ``deciding_policy``, and withheld writes.
        :param phase: Which enforcement point produced the ASK.
        :param content_preview: Content snapshot for the UI (truncated by the
            strategy).
        :param register: Seam: persist the elicitation row, called with
            ``(elicitation_id, task_id, params_json)``.
        :param emit: Seam: publish the ``response.elicitation_request`` event.
        :returns: The built :class:`ElicitationRequest` (already registered + emitted).
        """
        ...

    def apply_verdict(
        self,
        *,
        raw_verdict: str | None,
        result: PolicyResult,
        policy_engine: PolicyEngine,
    ) -> bool:
        """Parse the verdict and apply withheld writes only on approve.

        :param raw_verdict: The JSON-encoded MCP ``ElicitResult`` body delivered by
            the park callback, or ``None`` when no row was present on wake.
        :param result: The composed ASK result whose ``set_labels`` / ``state_updates``
            are applied only on approve.
        :param policy_engine: Engine used to apply the label / state writes.
        :returns: ``True`` only when the verdict's ``action`` is exactly
            ``"accept"``; ``False`` otherwise (decline / cancel / timeout / malformed).
        """
        ...


class DefaultApprovalStrategy:
    """The established in-process elicitation behavior, factored into the seam.

    Reproduces :func:`_await_elicitation` verbatim: :meth:`compose_ask` builds the
    :class:`ElicitationRequest` with the 1024-char preview truncation, serializes
    the params, registers the row, and emits the SSE event; :meth:`apply_verdict`
    fails closed via the core ``_parse_verdict`` and applies the withheld
    ``set_labels`` + ``state_updates`` through the engine **only on approve**
    (POLICIES.md §7.2). It delegates to the existing core helpers so there is one
    implementation of the wire shape and the §7.2 invariant, not a fork.
    """

    def compose_ask(
        self,
        *,
        elicitation_id: str,
        task_id: str,
        result: PolicyResult,
        phase: Phase,
        content_preview: str,
        register: _RegisterCallback,
        emit: _EmitCallback,
    ) -> ElicitationRequest:
        """See :meth:`ApprovalStrategy.compose_ask` — verbatim core behavior."""
        elicitation = ElicitationRequest(
            message=result.reason or "",
            phase=phase.value,
            policy_name=result.deciding_policy or "",
            content_preview=_truncate(content_preview, limit=1024),
        )
        params_json = build_elicitation_params_json(elicitation)
        register(elicitation_id, task_id, params_json)
        emit(build_elicitation_request_event(elicitation_id, elicitation))
        return elicitation

    def apply_verdict(
        self,
        *,
        raw_verdict: str | None,
        result: PolicyResult,
        policy_engine: PolicyEngine,
    ) -> bool:
        """See :meth:`ApprovalStrategy.apply_verdict` — verbatim core behavior."""
        approved = _parse_verdict(raw_verdict)
        if approved:
            # POLICIES.md §7.2: writes accumulated by ASKing policies land only on
            # approve. On refuse / cancel / timeout / malformed verdict we drop them
            # — a denied ASK must leave no trace.
            if result.set_labels:
                policy_engine.apply_label_writes(result.set_labels)
            if result.state_updates:
                policy_engine.apply_state_updates(result.state_updates)
        return approved


# Deploy-time registry. The server registers the live strategy at startup; until
# then callers fall back to :class:`DefaultApprovalStrategy` — the established
# in-process behavior — so the seam never changes the default path.
_strategy: ApprovalStrategy | None = None


def set_approval_strategy(strategy: ApprovalStrategy | None) -> None:
    """Register (or clear) the process-wide live :class:`ApprovalStrategy`."""
    global _strategy
    _strategy = strategy


def get_approval_strategy() -> ApprovalStrategy:
    """Return the registered :class:`ApprovalStrategy`, or the default.

    Falls back to a fresh :class:`DefaultApprovalStrategy` when none is registered,
    so the seam preserves today's behavior with no wiring required.
    """
    return _strategy if _strategy is not None else DefaultApprovalStrategy()


async def drive_elicitation(
    *,
    strategy: ApprovalStrategy,
    elicitation_id: str,
    task_id: str,
    result: PolicyResult,
    phase: Phase,
    content_preview: str,
    policy_engine: PolicyEngine,
    register: _RegisterCallback,
    emit: _EmitCallback,
    park: _ParkCallback,
) -> bool:
    """Drive one elicitation round-trip through ``strategy``; return True iff approved.

    The strategy-aware counterpart to :func:`_await_elicitation`: it composes the ask
    via :meth:`ApprovalStrategy.compose_ask`, resolves the per-policy ask timeout,
    parks for the verdict, then applies it via :meth:`ApprovalStrategy.apply_verdict`.
    Identical control flow + timeout semantics to the core helper, so swapping in
    :class:`DefaultApprovalStrategy` is a behavioral no-op.

    :param strategy: The :class:`ApprovalStrategy` to drive.
    :param elicitation_id: Pre-minted correlation id (the caller mints it; this
        helper parks on it).
    :param task_id: The parked sub-agent's task id.
    :param result: Engine-composed ASK result.
    :param phase: Enforcement point that produced the ASK.
    :param content_preview: Content snapshot for the UI.
    :param policy_engine: Engine — resolves the per-policy ``ask_timeout`` and
        applies writes on approve.
    :param register: Seam: register the pending elicitation row.
    :param emit: Seam: publish the elicitation event.
    :param park: Seam: block until a verdict arrives or the timeout elapses. Raises
        :class:`TimeoutError` on deadline expiry (→ refusal).
    :returns: ``True`` only on an exact ``action == "accept"`` verdict.
    """
    strategy.compose_ask(
        elicitation_id=elicitation_id,
        task_id=task_id,
        result=result,
        phase=phase,
        content_preview=content_preview,
        register=register,
        emit=emit,
    )
    effective_timeout = resolve_ask_timeout(policy_engine, result)
    try:
        raw_verdict = await park(elicitation_id, effective_timeout)
    except TimeoutError:
        return False
    return strategy.apply_verdict(
        raw_verdict=raw_verdict,
        result=result,
        policy_engine=policy_engine,
    )


__all__ = [
    "ApprovalStrategy",
    "DefaultApprovalStrategy",
    "drive_elicitation",
    "get_approval_strategy",
    "set_approval_strategy",
]
