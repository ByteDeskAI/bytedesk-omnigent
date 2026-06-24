"""Edge-path coverage for :mod:`omnigent.runner.cost_advisor` helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from omnigent.cost_plan import AdvisorVerdict
from omnigent.runner.cost_advisor import (
    _databricks_profile_for_spec,
    _persist_verdict_label,
    maybe_run_advisor,
)
from omnigent.spec.types import AgentSpec, ApiKeyAuth, DatabricksAuth, ExecutorSpec

_ANCHOR = "2026-06-10T00:00:00+00:00"
_TIERS_YAML: dict[str, Any] = {
    "mode": "optimize",
    "tiers": {"cheap": ["databricks-claude-haiku-4-5"]},
}


def _claude_spec(**config_extra: object) -> AgentSpec:
    config: dict[str, object] = {"harness": "claude-sdk", **config_extra}
    return AgentSpec(
        spec_version=1,
        name="orchestrator",
        executor=ExecutorSpec(type="omnigent", config=config),
    )


def test_databricks_profile_from_provider_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.runtime.workflow._resolve_provider_for_build",
        lambda _spec, harness_type: SimpleNamespace(kind="databricks", profile="dbx-prof"),
    )
    assert _databricks_profile_for_spec(_claude_spec()) == "dbx-prof"


def test_databricks_profile_none_for_non_databricks_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.runtime.workflow._resolve_provider_for_build",
        lambda _spec, harness_type: SimpleNamespace(kind="key", profile=None),
    )
    assert _databricks_profile_for_spec(_claude_spec()) is None


def test_databricks_profile_from_executor_auth() -> None:
    spec = AgentSpec(
        spec_version=1,
        name="orchestrator",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-sdk"},
            auth=DatabricksAuth(profile="auth-profile"),
        ),
    )
    assert _databricks_profile_for_spec(spec) == "auth-profile"


def test_databricks_profile_none_for_explicit_api_key_auth() -> None:
    spec = AgentSpec(
        spec_version=1,
        name="orchestrator",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-sdk"},
            auth=ApiKeyAuth(api_key="sk-test"),
        ),
    )
    assert _databricks_profile_for_spec(spec) is None


def test_databricks_profile_from_legacy_config_profile() -> None:
    assert _databricks_profile_for_spec(_claude_spec(profile="legacy-prof")) == "legacy-prof"


def test_databricks_profile_from_global_auth_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"auth": {"type": "databricks", "profile": "global-prof"}})
    )
    assert _databricks_profile_for_spec(_claude_spec()) == "global-prof"


def test_databricks_profile_resolution_fail_open_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("provider resolution failed")

    monkeypatch.setattr(
        "omnigent.runtime.workflow._resolve_provider_for_build",
        _boom,
    )
    assert _databricks_profile_for_spec(_claude_spec()) is None


@pytest.mark.asyncio
async def test_persist_verdict_label_returns_false_on_transport_error() -> None:
    verdict = AdvisorVerdict(
        tier="cheap",
        model="databricks-claude-haiku-4-5",
        applied=True,
        rationale="r",
        turn_anchor=_ANCHOR,
    )

    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_boom),
        base_url="http://omnigent.test",
    ) as client:
        ok = await _persist_verdict_label(verdict, "conv_x", client)

    assert ok is False


@pytest.mark.asyncio
async def test_maybe_run_advisor_returns_none_when_persist_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Judge:
        async def judge(self, *, query: str, turn_anchor: str) -> AdvisorVerdict:
            return AdvisorVerdict(
                tier="cheap",
                model="databricks-claude-haiku-4-5",
                applied=False,
                rationale="r",
                turn_anchor=turn_anchor,
            )

    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_boom),
        base_url="http://omnigent.test",
    ) as client:
        result = await maybe_run_advisor(
            spec=_claude_spec(cost_optimize=_TIERS_YAML),
            conversation_id="conv_x",
            turn_content=[{"type": "input_text", "text": "refactor auth"}],
            server_client=client,
            turn_anchor=_ANCHOR,
            harness="claude-sdk",
            judge=_Judge(),
        )

    assert result is None