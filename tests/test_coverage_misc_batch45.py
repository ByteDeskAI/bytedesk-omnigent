"""Batch-45 coverage for runner policy gate, adapter registry, and coordination lifecycle."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.coordination import lifecycle as coord_lifecycle
from omnigent.coordination.inprocess import InProcessBackplane
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters import clear_cache, get_adapter
from omnigent.policies.types import PolicyResult
from omnigent.runner.policy import (
    RunnerToolPolicyGate,
    _GatedPolicy,
    _selector_covers_tools,
    format_deny_text,
)
from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    Phase,
    PhaseSelector,
    PolicyAction,
    PolicySpec,
)


@pytest.fixture(autouse=True)
def _reset_coordination() -> None:
    coord_lifecycle.reset_for_tests()
    yield
    coord_lifecycle.reset_for_tests()
    clear_cache()


def _agent_with_policies(policies: list) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="policy-gate-test",
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
        guardrails=GuardrailsSpec(policies=policies),
    )


@pytest.mark.asyncio
async def test_runner_policy_gate_skips_non_function_policies() -> None:
    spec = _agent_with_policies(
        [
            PolicySpec(name="prompt_only", on=None),
            FunctionPolicySpec(
                name="tool_gate",
                on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
                function=FunctionRef(
                    path="omnigent.policies.builtins.cel.cel_policy",
                    arguments={
                        "expression": (
                            'event.type == "tool_call" && event.data.name == "blocked"'
                            ' ? {"result": "DENY", "reason": "blocked"}'
                            ' : {"result": "ALLOW"}'
                        ),
                        "reason": "blocked",
                    },
                ),
            ),
        ]
    )
    gate = RunnerToolPolicyGate.from_spec(spec)
    verdict = await gate.evaluate_tool_call("blocked", {})
    assert verdict.action == "deny"
    assert verdict.policy_name == "tool_gate"


@pytest.mark.asyncio
async def test_runner_policy_gate_self_selects_when_on_is_none() -> None:
    spec = _agent_with_policies(
        [
            FunctionPolicySpec(
                name="both_phases",
                on=None,
                function=FunctionRef(
                    path="omnigent.policies.builtins.cel.cel_policy",
                    arguments={
                        "expression": (
                            'event.type == "tool_result"'
                            ' ? {"result": "DENY", "reason": "deny results"}'
                            ' : {"result": "ALLOW"}'
                        ),
                        "reason": "deny results",
                    },
                ),
            ),
        ]
    )
    gate = RunnerToolPolicyGate.from_spec(spec)
    assert gate.is_empty is False
    output = await gate.evaluate_tool_result("any_tool", "raw output")
    assert "deny results" in output


@pytest.mark.asyncio
async def test_runner_policy_gate_allows_and_transforms_tool_result() -> None:
    class _RedactPolicy:
        async def evaluate(self, _ctx, _cfg):
            return PolicyResult(action=PolicyAction.ALLOW, reason=None, data="scrubbed")

        def reset_turn(self) -> None:
            return None

    gate = RunnerToolPolicyGate(
        [
            _GatedPolicy(
                name="redact", policy=_RedactPolicy(), phases=frozenset([Phase.TOOL_RESULT])
            )
        ]
    )
    output = await gate.evaluate_tool_result("tool", "secret")
    assert output == "scrubbed"


@pytest.mark.asyncio
async def test_runner_policy_gate_collapses_ask_on_tool_result() -> None:
    spec = _agent_with_policies(
        [
            FunctionPolicySpec(
                name="ask_gate",
                on=[PhaseSelector(phase=Phase.TOOL_RESULT, tool_name=None)],
                function=FunctionRef(
                    path="omnigent.policies.builtins.cel.cel_policy",
                    arguments={
                        "expression": (
                            'event.type == "tool_result"'
                            ' ? {"result": "ASK", "reason": "need approval"}'
                            ' : {"result": "ALLOW"}'
                        ),
                        "reason": "need approval",
                    },
                ),
            ),
        ]
    )
    gate = RunnerToolPolicyGate.from_spec(spec)
    output = await gate.evaluate_tool_result("tool", "done")
    assert "ask_gate" in output
    assert "need approval" in output


@pytest.mark.asyncio
async def test_runner_policy_gate_treats_policy_exception_as_deny() -> None:
    class _BoomPolicy:
        async def evaluate(self, _ctx, _cfg):
            raise RuntimeError("boom")

        def reset_turn(self) -> None:
            return None

    gate = RunnerToolPolicyGate(
        [_GatedPolicy(name="boom", policy=_BoomPolicy(), phases=frozenset([Phase.TOOL_CALL]))]
    )
    verdict = await gate.evaluate_tool_call("x", {})
    assert verdict.action == "deny"
    assert "boom" in (verdict.deny_text or "")


def test_runner_policy_gate_reset_turn_forwards() -> None:
    resets: list[str] = []

    class _Policy:
        def reset_turn(self) -> None:
            resets.append("ok")

        async def evaluate(self, _ctx, _cfg):
            return type("R", (), {"action": PolicyAction.ALLOW, "reason": None, "data": None})()

    gate = RunnerToolPolicyGate(
        [_GatedPolicy(name="p", policy=_Policy(), phases=frozenset([Phase.TOOL_CALL]))]
    )
    gate.reset_turn()
    assert resets == ["ok"]


def test_selector_covers_tools_only_tool_phases() -> None:
    assert _selector_covers_tools(Phase.TOOL_CALL) is True
    assert _selector_covers_tools(Phase.TOOL_RESULT) is True
    assert _selector_covers_tools(Phase.REQUEST) is False


def test_format_deny_text_includes_policy_and_reason() -> None:
    text = format_deny_text("gate", None)
    assert "gate" in text
    assert "policy denied" in text


def test_get_adapter_caches_instances() -> None:
    first = get_adapter("groq")
    second = get_adapter("groq")
    assert first is second


def test_get_adapter_openai_uses_subclass() -> None:
    from omnigent.llms.adapters.openai import OpenAIAdapter

    adapter = get_adapter("openai")
    assert isinstance(adapter, OpenAIAdapter)


def test_get_adapter_unknown_provider_raises() -> None:
    with pytest.raises(OmnigentError) as exc:
        get_adapter("not-a-provider")
    assert exc.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "provider",
    ["anthropic", "gemini", "bedrock", "vertex", "databricks", "deepseek", "xai"],
)
def test_get_adapter_lazy_imports_optional_providers(provider: str) -> None:
    adapter = get_adapter(provider)
    assert adapter is not None


def test_get_adapter_with_kwargs_skips_cache() -> None:
    first = get_adapter("ollama", base_url="http://127.0.0.1:11434/v1")
    second = get_adapter("ollama", base_url="http://127.0.0.1:11434/v1")
    assert first is not second


def test_schedule_backplane_noops_without_backplane() -> None:
    coord_lifecycle.schedule_backplane(AsyncMock())


@pytest.mark.asyncio
async def test_schedule_backplane_creates_task_on_same_loop() -> None:
    backplane = InProcessBackplane("replica-sched")
    coord_lifecycle._backplane = backplane
    coord_lifecycle._loop = asyncio.get_running_loop()
    coro = asyncio.sleep(0)
    coord_lifecycle.schedule_backplane(coro)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_fanout_pending_upsert_and_delete_publish() -> None:
    backplane = InProcessBackplane("replica-fanout")
    await backplane.start()
    coord_lifecycle._backplane = backplane
    coord_lifecycle._loop = asyncio.get_running_loop()

    coord_lifecycle.fanout_pending_upsert("conv_a", "elicit_1", {"type": "request"})
    coord_lifecycle.fanout_pending_delete("conv_a", "elicit_1")
    await asyncio.sleep(0.1)
    await backplane.stop()


@pytest.mark.asyncio
async def test_start_and_stop_coordination_wires_backplane() -> None:
    backplane = await coord_lifecycle.start_coordination()
    assert coord_lifecycle.get_active_backplane() is backplane
    await coord_lifecycle.stop_coordination()
    assert coord_lifecycle.get_active_backplane() is None


@pytest.mark.asyncio
async def test_fanout_listener_ignores_malformed_messages() -> None:
    bp = InProcessBackplane("replica-listener")
    await bp.start()
    listener = asyncio.create_task(coord_lifecycle._fanout_listener(bp))
    await asyncio.sleep(0.05)
    await bp.publish("omnigent.coord.fanout.pending.upsert", b"not-json")
    await bp.publish(
        "omnigent.coord.fanout.pending.upsert",
        json.dumps(
            {
                "kind": "pending.upsert",
                "conversation_id": "",
                "elicitation_id": "elicit_x",
                "event": {},
                "origin": "peer",
            }
        ).encode("utf-8"),
    )
    await asyncio.sleep(0.1)
    listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await listener
    await bp.stop()


def test_runner_policy_gate_empty_spec_has_no_policies() -> None:
    gate = RunnerToolPolicyGate.from_spec(
        AgentSpec(
            spec_version=1,
            name="no-guardrails",
            executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
        )
    )
    assert gate.is_empty is True


@pytest.mark.asyncio
async def test_runner_policy_gate_empty_returns_original_output() -> None:
    gate = RunnerToolPolicyGate([])
    assert (await gate.evaluate_tool_call("tool", {})).action == "allow"
    assert await gate.evaluate_tool_result("tool", "payload") == "payload"


def test_schedule_backplane_logs_threadsafe_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    backplane = MagicMock()
    loop = MagicMock()

    class _Future:
        def add_done_callback(self, cb):
            fut = MagicMock()
            fut.exception.return_value = RuntimeError("peer down")
            cb(fut)

    monkeypatch.setattr(coord_lifecycle, "_backplane", backplane)
    monkeypatch.setattr(coord_lifecycle, "_loop", loop)
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda _coro, _loop: _Future())
    coord_lifecycle.schedule_backplane(AsyncMock())
