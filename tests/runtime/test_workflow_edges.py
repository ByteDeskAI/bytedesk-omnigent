"""Edge-case unit coverage for :mod:`omnigent.runtime.workflow`."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml

from omnigent.entities import CompactionData, ConversationItem, MessageData, NewConversationItem
from omnigent.entities.pagination import paginate_in_memory
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.datamodel import OSEnvSpec
from omnigent.llms import Client as LLMClient
from omnigent.onboarding.provider_config import FamilyConfig, ProviderEntry
from omnigent.onboarding.ucode_state import UcodeAgentState
from omnigent.runtime import workflow as wf
from omnigent.runtime.compaction import CompactionResult, SummaryMetadata, _CompactionState
from omnigent.runtime.workflow import (
    _add_claude_sdk_skills_env,
    _apply_provider_family,
    _apply_provider_to_openai_agents,
    _apply_provider_to_pi,
    _build_claude_sdk_spawn_env,
    _build_codex_spawn_env,
    _build_cursor_spawn_env,
    _build_openai_agents_sdk_spawn_env,
    _build_pi_spawn_env,
    _catalog_default_model,
    _find_spec_by_name,
    _get_llm_client,
    _get_runner_client_for_compaction,
    _inject_ucode_agent_state,
    _load_global_auth,
    _load_initial_history,
    _maybe_persist_compaction_item,
    _optional_provider_family,
    _prepare_messages,
    _provider_auth_command,
    _route_databricks_model_for_compaction,
    _serialize_os_env,
    _serialize_retry_policy,
    compact_conversation_now,
    configure_agent_harness_with_provider,
    configure_agent_harness_with_ucode,
)
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    ExecutorSpec,
    LLMConfig,
    ProviderAuth,
    RetryPolicy,
)


def _minimal_spec(
    *,
    harness: str = "claude-sdk",
    model: str | None = None,
    name: str = "test-agent",
    auth: Any = None,
    config_extra: dict[str, object] | None = None,
    os_env: OSEnvSpec | None = None,
    llm_retry: RetryPolicy | None = None,
    sub_agents: list[AgentSpec] | None = None,
) -> AgentSpec:
    config: dict[str, object] = {"harness": harness}
    if model is not None:
        config["model"] = model
    if config_extra:
        config.update(config_extra)
    return AgentSpec(
        spec_version=1,
        name=name,
        instructions="test",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model or "gpt-test", retry=llm_retry) if model or llm_retry else None,
        os_env=os_env,
        sub_agents=sub_agents or [],
    )


class _HistoryStore:
    """Minimal conversation store for history-loader tests."""

    def __init__(self, items: list[ConversationItem]) -> None:
        self._items = items
        self.appended: list[NewConversationItem] = []

    def list_items(
        self,
        conversation_id: str,
        *,
        type: str | None = None,
        order: str = "asc",
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        **kwargs: Any,
    ):
        del conversation_id, kwargs
        items = [item for item in self._items if type is None or item.type == type]
        return paginate_in_memory(
            items,
            lambda item: item.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    def append(self, conversation_id: str, items: list[NewConversationItem]) -> None:
        del conversation_id
        self.appended.extend(items)


def _message_item(item_id: str, text: str, *, created_at: int = 1) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=created_at,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


# ── LLM client + runner routing ────────────────────────────────


def test_get_llm_client_lazy_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared LLM client is constructed once on first use."""
    created: list[LLMClient] = []

    class _FakeClient(LLMClient):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    monkeypatch.setattr(wf, "_llm_client", None)
    monkeypatch.setattr(wf, "LLMClient", _FakeClient)
    first = _get_llm_client()
    second = _get_llm_client()
    assert first is second
    assert len(created) == 1


def test_get_runner_client_for_compaction_returns_none_without_conversation() -> None:
    assert _get_runner_client_for_compaction(None) is None


def test_get_runner_client_for_compaction_returns_none_without_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: None)
    assert _get_runner_client_for_compaction("conv_x") is None


@pytest.mark.asyncio
async def test_get_runner_client_for_compaction_returns_client_when_routed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = httpx.AsyncClient()
    router = SimpleNamespace(
        client_for_existing_conversation=lambda conv_id: SimpleNamespace(client=client)
    )
    monkeypatch.setattr("omnigent.runtime.get_runner_router", lambda: router)
    assert _get_runner_client_for_compaction("conv_x") is client
    await client.aclose()


# ── ucode + provider helpers ───────────────────────────────────


def test_configure_agent_harness_with_ucode_noops_when_state_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env: dict[str, str] = {}
    monkeypatch.setattr(
        wf,
        "get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(wf, "read_ucode_state", lambda url: None)
    configure_agent_harness_with_ucode(env, "oss", harness_type="claude-sdk")
    assert env == {}


def test_inject_ucode_agent_state_sets_refresh_interval() -> None:
    env: dict[str, str] = {}
    state = UcodeAgentState(
        model="databricks-claude-sonnet-4-6",
        base_url="https://example.databricks.com/v1",
        auth_command="printf token",
        auth_refresh_interval_ms=900000,
    )
    _inject_ucode_agent_state(
        env,
        state,
        model_key="HARNESS_CODEX_MODEL",
        base_url_key="HARNESS_CODEX_GATEWAY_BASE_URL",
        base_url_family=None,
        base_urls_key=None,
        host_key="HARNESS_CODEX_GATEWAY_HOST",
        auth_key="HARNESS_CODEX_GATEWAY_AUTH_COMMAND",
        refresh_key="HARNESS_CODEX_GATEWAY_AUTH_REFRESH_INTERVAL_MS",
        workspace_url="https://example.databricks.com",
    )
    assert env["HARNESS_CODEX_GATEWAY_AUTH_REFRESH_INTERVAL_MS"] == "900000"


def test_provider_auth_command_raises_without_credential() -> None:
    family = FamilyConfig(base_url="https://example.com/v1")
    with pytest.raises(OmnigentError, match="no credential"):
        _provider_auth_command(family)


def test_configure_agent_harness_with_provider_raises_for_missing_family() -> None:
    entry = ProviderEntry(
        name="openai-only",
        kind="key",
        families={
            "openai": FamilyConfig(
                base_url="https://openai.example.com/v1",
                api_key="sk-test",
            )
        },
    )
    with pytest.raises(OmnigentError, match="no 'anthropic' family"):
        configure_agent_harness_with_provider({}, entry, harness_type="claude-sdk")


def test_catalog_default_model_unknown_family_returns_none() -> None:
    assert _catalog_default_model("not-a-real-family") is None


def test_apply_provider_family_raises_when_no_model_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wf, "_catalog_default_model", lambda _family: None)
    family = FamilyConfig(base_url="https://anthropic.example.com/v1", api_key="sk-ant")
    with pytest.raises(OmnigentError, match="No model resolved"):
        _apply_provider_family({}, "claude-sdk", family)


def test_apply_provider_to_openai_agents_uses_auth_command_for_dynamic_tokens() -> None:
    env: dict[str, str] = {}
    family = FamilyConfig(
        base_url="https://gateway.example.com/v1",
        auth_command="my-cli print-token",
        models={"default": "gpt-test"},
    )
    _apply_provider_to_openai_agents(env, family)
    assert env["HARNESS_OPENAI_AGENTS_GATEWAY_AUTH_COMMAND"] == "my-cli print-token"
    assert env["HARNESS_OPENAI_AGENTS_GATEWAY_HOST"] == "https://gateway.example.com"


def test_apply_provider_to_openai_agents_maps_chat_wire_api() -> None:
    env: dict[str, str] = {}
    family = FamilyConfig(
        base_url="https://gateway.example.com/v1",
        api_key="sk-test",
        wire_api="chat",
        models={"default": "gpt-test"},
    )
    _apply_provider_to_openai_agents(env, family)
    assert env["HARNESS_OPENAI_AGENTS_USE_RESPONSES"] == "false"


def test_apply_provider_to_openai_agents_raises_when_no_model_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wf, "_catalog_default_model", lambda _family: None)
    family = FamilyConfig(
        base_url="https://gateway.example.com/v1",
        api_key="sk-test",
    )
    with pytest.raises(OmnigentError, match="openai-agents-sdk"):
        _apply_provider_to_openai_agents({}, family)


def test_optional_provider_family_returns_none_on_resolution_error() -> None:
    entry = ProviderEntry(
        name="missing-env",
        kind="key",
        families={
            "anthropic": FamilyConfig(
                base_url="https://anthropic.example.com/v1",
                api_key_ref="env:DEFINITELY_MISSING_ANTHROPIC_KEY",
            )
        },
    )
    assert _optional_provider_family(entry, "anthropic") is None


def test_apply_provider_to_pi_openai_only_family() -> None:
    entry = ProviderEntry(
        name="openai-only",
        kind="key",
        families={
            "openai": FamilyConfig(
                base_url="https://openai.example.com/v1",
                api_key="sk-oai",
                models={"default": "gpt-test"},
            )
        },
    )
    env: dict[str, str] = {}
    _apply_provider_to_pi(env, entry)
    assert json.loads(env["HARNESS_PI_GATEWAY_BASE_URLS"]) == {
        "openai": "https://openai.example.com/v1"
    }
    assert env["HARNESS_PI_GATEWAY_AUTH_COMMAND"] == "printf %s sk-oai"


def test_apply_provider_to_pi_raises_when_no_family_resolves() -> None:
    entry = ProviderEntry(
        name="broken",
        kind="key",
        families={
            "anthropic": FamilyConfig(
                base_url="https://anthropic.example.com/v1",
                api_key_ref="env:MISSING_ANTHROPIC_FOR_PI",
            ),
            "openai": FamilyConfig(
                base_url="https://openai.example.com/v1",
                api_key_ref="env:MISSING_OPENAI_FOR_PI",
            ),
        },
    )
    with pytest.raises(OmnigentError, match="no family whose credentials resolve"):
        _apply_provider_to_pi({}, entry)


def test_apply_provider_to_pi_raises_when_no_model_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wf, "_catalog_default_model", lambda _family: None)
    entry = ProviderEntry(
        name="openai-no-model",
        kind="key",
        families={
            "openai": FamilyConfig(
                base_url="https://openai.example.com/v1",
                api_key="sk-oai",
            )
        },
    )
    with pytest.raises(OmnigentError, match="No model resolved"):
        _apply_provider_to_pi({}, entry)


# ── spawn-env optional payloads ────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CURSOR_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_add_claude_sdk_skills_env_threads_bundle_dir(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    _add_claude_sdk_skills_env(env, _minimal_spec(), tmp_path)
    assert env["HARNESS_CLAUDE_SDK_BUNDLE_DIR"] == str(tmp_path)


def test_claude_sdk_spawn_env_threads_os_env_retry_and_permission_mode() -> None:
    spec = _minimal_spec(
        os_env=OSEnvSpec(type="caller_process", fork=True),
        llm_retry=RetryPolicy(max_retries=3),
        config_extra={"permission_mode": "default"},
    )
    env = _build_claude_sdk_spawn_env(spec, workdir=Path("/bundle"))
    assert "HARNESS_CLAUDE_SDK_OS_ENV" in env
    assert "HARNESS_CLAUDE_SDK_RETRY_POLICY" in env
    assert env["HARNESS_CLAUDE_SDK_PERMISSION_MODE"] == "default"
    assert env["HARNESS_CLAUDE_SDK_BUNDLE_DIR"] == "/bundle"


def test_codex_spawn_env_threads_bundle_os_env_and_retry(tmp_path: Path) -> None:
    spec = _minimal_spec(
        harness="codex",
        model="gpt-test",
        os_env=OSEnvSpec(type="caller_process"),
        llm_retry=RetryPolicy(max_retries=2),
    )
    env = _build_codex_spawn_env(spec, workdir=tmp_path)
    assert env["HARNESS_CODEX_BUNDLE_DIR"] == str(tmp_path)
    assert "HARNESS_CODEX_OS_ENV" in env
    assert "HARNESS_CODEX_RETRY_POLICY" in env


def test_pi_spawn_env_threads_bundle_and_os_env(tmp_path: Path) -> None:
    spec = _minimal_spec(
        harness="pi",
        model="gpt-test",
        os_env=OSEnvSpec(type="caller_process"),
    )
    env = _build_pi_spawn_env(spec, workdir=tmp_path)
    assert env["HARNESS_PI_BUNDLE_DIR"] == str(tmp_path)
    assert "HARNESS_PI_OS_ENV" in env


def test_cursor_spawn_env_threads_os_env() -> None:
    spec = _minimal_spec(
        harness="cursor",
        model="gpt-test",
        os_env=OSEnvSpec(type="caller_process"),
    )
    env = _build_cursor_spawn_env(spec)
    assert "HARNESS_CURSOR_OS_ENV" in env


def test_openai_agents_provider_branch_honors_use_responses_override(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "vendor-openai": {
                        "kind": "key",
                        "default": True,
                        "openai": {
                            "base_url": "https://openai.example.com/v1",
                            "api_key": "sk-oai",
                            "models": {"default": "gpt-test"},
                            "wire_api": "responses",
                        },
                    }
                }
            }
        )
    )
    spec = _minimal_spec(
        harness="openai-agents",
        config_extra={"use_responses": False},
    )
    env = _build_openai_agents_sdk_spawn_env(spec)
    assert env["HARNESS_OPENAI_AGENTS_USE_RESPONSES"] == "false"


def test_load_global_auth_handles_corrupt_yaml(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(":\n  bad: [")
    assert _load_global_auth() is None


def test_load_global_auth_rejects_empty_api_key(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"auth": {"type": "api_key", "api_key": ""}})
    )
    assert _load_global_auth() is None


def test_load_global_auth_unknown_type_returns_none(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"auth": {"type": "oauth", "token": "x"}})
    )
    assert _load_global_auth() is None


def test_serialize_os_env_encodes_spec() -> None:
    payload = _serialize_os_env(OSEnvSpec(type="caller_process", fork=True))
    assert payload is not None
    assert json.loads(payload)["fork"] is True


def test_serialize_retry_policy_omits_defaults() -> None:
    assert _serialize_retry_policy(RetryPolicy()) is None
    custom = _serialize_retry_policy(RetryPolicy(max_retries=1))
    assert custom is not None
    assert json.loads(custom)["max_retries"] == 1


# ── message preparation + spec lookup ──────────────────────────


def test_prepare_messages_resolves_content_when_stores_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = [_message_item("m1", "hello")]
    resolved = [_message_item("m1", "resolved")]
    monkeypatch.setattr(wf, "get_file_store", lambda: object())
    monkeypatch.setattr(wf, "get_artifact_store", lambda: object())
    monkeypatch.setattr(wf, "resolve_content_references", lambda *args, **kwargs: resolved)
    monkeypatch.setattr(wf, "build_instructions", lambda *args, **kwargs: "sys")
    monkeypatch.setattr(wf, "history_to_input_items", lambda items: [{"role": "user"}])
    monkeypatch.setattr(wf, "count_tokens", lambda msgs, model: 3)

    _sys, messages, tokens = _prepare_messages(
        _minimal_spec(),
        LLMConfig(model="gpt-test"),
        history,
        None,
        [],
        _CompactionState(
            context_window=None,
            last_summary=None,
            config=None,
            model="gpt-test",
            connection={},
            conversation_id="conv_1",
        ),
        {},
        conversation_id="conv_1",
    )
    assert messages == [{"role": "user"}]
    assert tokens == 3


def test_find_spec_by_name_searches_nested_sub_agents() -> None:
    child = _minimal_spec(name="child-agent")
    parent = _minimal_spec(
        sub_agents=[_minimal_spec(name="wrapper", sub_agents=[child])],
    )
    found = _find_spec_by_name(parent, "child-agent")
    assert found is child
    assert _find_spec_by_name(parent, "missing") is None


# ── compaction history + persistence ───────────────────────────


def test_load_initial_history_ignores_broken_compaction_item() -> None:
    broken = ConversationItem(
        id="cmp_bad",
        type="compaction",
        status="completed",
        response_id="task_bad",
        created_at=1,
        data=CompactionData(summary="", last_item_id="synthetic_x", model="m", token_count=1),
    )
    visible = _message_item("msg_1", "keep me", created_at=2)
    loaded = _load_initial_history(_HistoryStore([broken, visible]), "conv_1")  # type: ignore[arg-type]
    assert [item.id for item in loaded.items] == ["msg_1"]


def test_load_initial_history_rehydrates_after_valid_compaction() -> None:
    old = _message_item("msg_old", "old", created_at=1)
    compaction = ConversationItem(
        id="cmp_ok",
        type="compaction",
        status="completed",
        response_id="task_ok",
        created_at=2,
        data=CompactionData(
            summary="older context summarized",
            last_item_id="msg_old",
            model="gpt-test",
            token_count=12,
        ),
    )
    recent = _message_item("msg_new", "recent", created_at=3)
    loaded = _load_initial_history(
        _HistoryStore([old, compaction, recent]),  # type: ignore[arg-type]
        "conv_1",
    )
    assert any(item.id == "msg_new" for item in loaded.items)
    assert loaded.last_compaction_created_at == 2


def test_route_databricks_model_for_compaction_prefixes_provider() -> None:
    cfg = LLMConfig(model="databricks-gpt-5-4", extra={})
    routed = _route_databricks_model_for_compaction(cfg)
    assert routed.model == "databricks/databricks-gpt-5-4"


def test_maybe_persist_compaction_item_skips_broken_summary() -> None:
    store = _HistoryStore([])
    _maybe_persist_compaction_item(
        SummaryMetadata(text="", last_item_id="msg_1", model="m", token_count=1),
        "task_1",
        "conv_1",
        store,  # type: ignore[arg-type]
    )
    assert store.appended == []


def test_maybe_persist_compaction_item_is_idempotent_for_same_task() -> None:
    store = _HistoryStore(
        [
            ConversationItem(
                id="cmp_existing",
                type="compaction",
                status="completed",
                response_id="task_same",
                created_at=1,
                data=CompactionData(
                    summary="already there",
                    last_item_id="msg_1",
                    model="gpt-test",
                    token_count=4,
                ),
            )
        ]
    )
    summary = SummaryMetadata(text="new summary", last_item_id="msg_2", model="gpt-test", token_count=8)
    _maybe_persist_compaction_item(summary, "task_same", "conv_1", store)  # type: ignore[arg-type]
    assert store.appended == []


@pytest.mark.asyncio
async def test_compact_conversation_now_returns_empty_for_no_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wf, "get_conversation_store", lambda: _HistoryStore([]))
    result = await compact_conversation_now(
        task_id="task_empty",
        conversation_id="conv_empty",
        spec=_minimal_spec(),
        llm_config=LLMConfig(model="gpt-test"),
    )
    assert result == CompactionResult(messages=[], summary_metadata=None)


@pytest.mark.asyncio
async def test_compact_conversation_now_persists_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _HistoryStore([_message_item("msg_1", "hello")])
    summary = SummaryMetadata(
        text="summary text",
        last_item_id="msg_1",
        model="gpt-test",
        token_count=10,
    )

    async def _fake_compact(*args: Any, **kwargs: Any) -> CompactionResult:
        del args, kwargs
        return CompactionResult(messages=[{"role": "user", "content": "hello"}], summary_metadata=summary)

    monkeypatch.setattr(wf, "get_conversation_store", lambda: store)
    monkeypatch.setattr(wf, "compact", _fake_compact)
    monkeypatch.setattr(wf, "_get_llm_client", lambda: MagicMock())
    monkeypatch.setattr(wf, "_get_runner_client_for_compaction", lambda _cid: None)
    monkeypatch.setattr(
        "omnigent.llms.context_window.get_model_context_window",
        lambda _model: 128000,
    )
    monkeypatch.setattr(wf, "build_instructions", lambda *a, **k: "sys")
    monkeypatch.setattr(wf, "history_to_input_items", lambda _h: [{"role": "user"}])
    monkeypatch.setattr(wf, "count_tokens", lambda _m, _model: 5)
    monkeypatch.setattr(wf, "get_file_store", lambda: None)
    monkeypatch.setattr(wf, "get_artifact_store", lambda: None)

    result = await compact_conversation_now(
        task_id="task_ok",
        conversation_id="conv_ok",
        spec=_minimal_spec(),
        llm_config=LLMConfig(model="gpt-test"),
        preserve_recent_window=1,
    )
    assert result.summary_metadata == summary
    assert len(store.appended) == 1
    assert store.appended[0].type == "compaction"


@pytest.mark.asyncio
async def test_compact_conversation_now_raises_when_summary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _HistoryStore([_message_item("msg_1", "hello")])

    async def _fake_compact(*args: Any, **kwargs: Any) -> CompactionResult:
        del args, kwargs
        return CompactionResult(messages=[{"role": "user"}], summary_metadata=None)

    monkeypatch.setattr(wf, "get_conversation_store", lambda: store)
    monkeypatch.setattr(wf, "compact", _fake_compact)
    monkeypatch.setattr(wf, "_get_llm_client", lambda: MagicMock())
    monkeypatch.setattr(wf, "_get_runner_client_for_compaction", lambda _cid: None)
    monkeypatch.setattr(
        "omnigent.llms.context_window.get_model_context_window",
        lambda _model: 128000,
    )
    monkeypatch.setattr(wf, "build_instructions", lambda *a, **k: "sys")
    monkeypatch.setattr(wf, "history_to_input_items", lambda _h: [{"role": "user"}])
    monkeypatch.setattr(wf, "count_tokens", lambda _m, _model: 5)
    monkeypatch.setattr(wf, "get_file_store", lambda: None)
    monkeypatch.setattr(wf, "get_artifact_store", lambda: None)

    with pytest.raises(OmnigentError) as exc_info:
        await compact_conversation_now(
            task_id="task_fail",
            conversation_id="conv_fail",
            spec=_minimal_spec(),
            llm_config=LLMConfig(model="databricks-gpt-5-4"),
            model_override="databricks-gpt-5-4-mini",
        )
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR