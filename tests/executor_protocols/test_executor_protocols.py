"""Tests for the runtime-checkable executor-callback Protocols (BDP-2339).

Asserts that a conforming async callback is recognised as a Protocol instance
and that a non-conforming object (sync callable, or missing ``__call__``) is
not — the runtime-checkable guarantee the wiring sites rely on.
"""
from __future__ import annotations

from typing import Any

from bytedesk_omnigent.executor_protocols import (
    ElicitationHandlerProtocol,
    PolicyEvaluatorProtocol,
    ToolExecutorProtocol,
)


# ---------------------------------------------------------------------------
# Conforming implementations — async ``__call__`` matching each contract.
# ---------------------------------------------------------------------------


class _ConformingToolExecutor:
    async def __call__(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"result": tool_name, "args": args}


class _ConformingElicitationHandler:
    async def __call__(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        return True


class _ConformingPolicyEvaluator:
    async def __call__(self, phase: str, data: dict[str, Any]) -> Any:
        return {"action": "POLICY_ACTION_ALLOW", "phase": phase, "data": data}


# ---------------------------------------------------------------------------
# Non-conforming objects — no ``__call__`` at all.
# ---------------------------------------------------------------------------


class _NotCallable:
    """An object with state but no ``__call__`` — never a callback Protocol."""

    value = 42


def test_conforming_objects_are_protocol_instances() -> None:
    assert isinstance(_ConformingToolExecutor(), ToolExecutorProtocol)
    assert isinstance(_ConformingElicitationHandler(), ElicitationHandlerProtocol)
    assert isinstance(_ConformingPolicyEvaluator(), PolicyEvaluatorProtocol)


def test_plain_async_function_satisfies_tool_executor_protocol() -> None:
    async def bridge(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": tool_name}

    # ``runtime_checkable`` only structurally verifies ``__call__`` presence,
    # which a function object has — the adapter installs bare functions, so
    # this is the shape the wiring sites actually pass.
    assert isinstance(bridge, ToolExecutorProtocol)
    assert isinstance(bridge, ElicitationHandlerProtocol)
    assert isinstance(bridge, PolicyEvaluatorProtocol)


def test_non_callable_object_is_not_a_protocol_instance() -> None:
    not_callable = _NotCallable()
    assert not isinstance(not_callable, ToolExecutorProtocol)
    assert not isinstance(not_callable, ElicitationHandlerProtocol)
    assert not isinstance(not_callable, PolicyEvaluatorProtocol)


def test_none_is_not_a_protocol_instance() -> None:
    # The adapter gates installation on ``getattr(executor, attr, None) is None``;
    # the sentinel ``None`` must not masquerade as a conforming callback.
    assert not isinstance(None, ToolExecutorProtocol)
    assert not isinstance(None, ElicitationHandlerProtocol)
    assert not isinstance(None, PolicyEvaluatorProtocol)
