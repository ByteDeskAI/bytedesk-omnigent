"""Batch-21 coverage for inner harness wraps and small transport/policy gaps."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from bytedesk_omnigent.harnesses import hermes_native_harness
from omnigent.db.converters import sql_agent_to_entity
from omnigent.db.db_models import SqlAgent
from omnigent.entities import Agent
from omnigent.inner import (
    antigravity_harness,
    claude_native_harness,
    codex_native_harness,
    databricks_supervisor_harness,
    grok_native_harness,
    pi_native_harness,
)
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.policies.enforcement import _enforce_policy
from omnigent.spec.types import Phase, PolicyAction

_HARNESS_API_PATHS = {"/health", "/v1/sessions/{conversation_id}/events"}


def _assert_harness_routes(app: Any) -> None:
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert paths >= _HARNESS_API_PATHS


# ── omnigent/inner/*_native_harness.py ───────────────────────────────────────


@pytest.mark.parametrize(
    ("registry_key", "module_path", "factory_module"),
    [
        ("claude-native", "omnigent.inner.claude_native_harness", claude_native_harness),
        ("codex-native", "omnigent.inner.codex_native_harness", codex_native_harness),
        ("pi-native", "omnigent.inner.pi_native_harness", pi_native_harness),
        ("grok-native", "omnigent.inner.grok_native_harness", grok_native_harness),
        ("grok", "omnigent.inner.grok_native_harness", grok_native_harness),
    ],
)
def test_native_harness_registry_and_create_app(
    registry_key: str,
    module_path: str,
    factory_module: Any,
) -> None:
    """Each native wrap is registered and exposes the harness API subset."""
    assert _HARNESS_MODULES.get(registry_key) == module_path
    _assert_harness_routes(factory_module.create_app())


def test_claude_native_executor_factory() -> None:
    """The claude-native factory constructs a bridge executor."""
    with patch("omnigent.inner.claude_native_harness.ClaudeNativeExecutor") as ctor:
        claude_native_harness._build_claude_native_executor()
    ctor.assert_called_once_with()


def test_codex_native_executor_factory() -> None:
    """The codex-native factory constructs a bridge executor."""
    with patch("omnigent.inner.codex_native_harness.CodexNativeExecutor") as ctor:
        codex_native_harness._build_codex_native_executor()
    ctor.assert_called_once_with()


def test_pi_native_executor_factory() -> None:
    """The pi-native factory constructs a bridge executor."""
    with patch("omnigent.inner.pi_native_harness.PiNativeExecutor") as ctor:
        pi_native_harness._build_pi_native_executor()
    ctor.assert_called_once_with()


def test_grok_native_executor_factory_respects_model_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_GROK_MODEL`` threads into ``GrokNativeExecutor``."""
    monkeypatch.delenv("HARNESS_GROK_MODEL", raising=False)
    with patch("omnigent.inner.grok_native_harness.GrokNativeExecutor") as ctor:
        grok_native_harness._build_grok_native_executor()
    ctor.assert_called_once_with(model=None)

    monkeypatch.setenv("HARNESS_GROK_MODEL", "grok-build")
    with patch("omnigent.inner.grok_native_harness.GrokNativeExecutor") as ctor:
        grok_native_harness._build_grok_native_executor()
    ctor.assert_called_once_with(model="grok-build")


# ── omnigent/inner/antigravity_harness.py ────────────────────────────────────


def test_antigravity_harness_create_app() -> None:
    """Antigravity wrap builds the standard harness routes."""
    assert _HARNESS_MODULES.get("antigravity") == "omnigent.inner.antigravity_harness"
    paths = set(antigravity_harness.create_app().openapi().get("paths", {}).keys())
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_antigravity_executor_factory_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_ANTIGRAVITY_*`` env vars thread into ``AntigravityExecutor``."""
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_MODEL", "gemini-flash")
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_API_KEY", "key-1")
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_VERTEX", "yes")
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_PROJECT", "proj-1")
    monkeypatch.setenv("HARNESS_ANTIGRAVITY_LOCATION", "us-east1")
    with patch("omnigent.inner.antigravity_harness.AntigravityExecutor") as ctor:
        antigravity_harness._build_antigravity_executor()
    ctor.assert_called_once_with(
        model="gemini-flash",
        api_key="key-1",
        vertex=True,
        project="proj-1",
        location="us-east1",
    )


# ── omnigent/inner/databricks_supervisor_harness.py ──────────────────────────


def test_databricks_supervisor_harness_registry_and_create_app() -> None:
    """Supervisor wrap is registered and exposes harness routes."""
    assert (
        _HARNESS_MODULES.get("databricks_supervisor")
        == "omnigent.inner.databricks_supervisor_harness"
    )
    _assert_harness_routes(databricks_supervisor_harness.create_app())


def test_databricks_supervisor_executor_factory() -> None:
    """The supervisor factory constructs an inner ``SupervisorExecutor``."""
    with patch(
        "omnigent.inner.databricks_supervisor_harness.SupervisorExecutor"
    ) as ctor:
        databricks_supervisor_harness._build_supervisor_executor()
    ctor.assert_called_once_with()


# ── bytedesk_omnigent/harnesses/hermes_native_harness.py ───────────────────


def test_hermes_native_harness_create_app() -> None:
    """Hermes wrap builds the standard harness routes."""
    assert (
        _HARNESS_MODULES.get("hermes")
        == "bytedesk_omnigent.harnesses.hermes_native_harness"
    )
    _assert_harness_routes(hermes_native_harness.create_app())


def test_hermes_native_executor_factory_respects_model_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HARNESS_HERMES_MODEL`` threads into ``HermesNativeExecutor``."""
    monkeypatch.delenv("HARNESS_HERMES_MODEL", raising=False)
    with patch(
        "bytedesk_omnigent.harnesses.hermes_native_harness.HermesNativeExecutor"
    ) as ctor:
        hermes_native_harness._build_hermes_native_executor()
    ctor.assert_called_once_with(model=None)

    monkeypatch.setenv("HARNESS_HERMES_MODEL", "hermes-model")
    with patch(
        "bytedesk_omnigent.harnesses.hermes_native_harness.HermesNativeExecutor"
    ) as ctor:
        hermes_native_harness._build_hermes_native_executor()
    ctor.assert_called_once_with(model="hermes-model")


# ── omnigent/runtime/policies/enforcement.py ───────────────────────────────


@pytest.mark.asyncio
async def test_enforce_policy_delegates_to_engine() -> None:
    """``_enforce_policy`` forwards evaluation to the engine."""
    expected = PolicyResult(action=PolicyAction.ALLOW)
    engine = AsyncMock()
    engine.evaluate.return_value = expected
    ctx = EvaluationContext(phase=Phase.REQUEST, content="ping", tool_name=None)
    result = await _enforce_policy(engine, ctx)
    engine.evaluate.assert_awaited_once_with(ctx)
    assert result is expected


# ── omnigent/db/converters.py ────────────────────────────────────────────────


def test_sql_agent_to_entity_maps_all_fields() -> None:
    """ORM rows convert to :class:`Agent` entities with full fidelity."""
    row = SqlAgent(
        id="ag_batch21",
        created_at=1700000000,
        name="coverage-agent",
        bundle_location="ag_batch21/sha",
        version=1,
        description="batch21",
        updated_at=1700000001,
        session_id="conv_batch21",
    )
    entity = sql_agent_to_entity(row)
    assert isinstance(entity, Agent)
    assert entity.id == "ag_batch21"
    assert entity.name == "coverage-agent"
    assert entity.bundle_location == "ag_batch21/sha"
    assert entity.version == 1
    assert entity.description == "batch21"
    assert entity.updated_at == 1700000001
    assert entity.session_id == "conv_batch21"
