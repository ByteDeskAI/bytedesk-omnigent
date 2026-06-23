"""Edge-case coverage for :mod:`omnigent.spec.omnigent` helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnigent.errors import OmnigentError
from omnigent.inner.datamodel import AgentDef, OSEnvSpec
from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec
from omnigent.inner.tools import AgentTool, FunctionTool, MCPTool
from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    LLMConfig,
    LocalToolInfo,
    MCPServerConfig,
    ToolRuntime,
)
from omnigent.spec.omnigent import (
    _agent_tool_to_sub_spec,
    _is_cancellable_function_path,
    _mcp_server_to_mcp_tool,
    _recover_callable_path,
    _reject_unsupported_concepts,
    _resolve_dotted_attr,
    _resolve_dotted_callable,
    _resolve_inline_agent_tool_os_env,
    _resolve_profile_to_connection,
    _translate_function_policy_yaml,
    _translate_function_tool_from_def,
    _translate_labels_yaml,
    _translate_mcp_tool_from_def,
    _translate_policies_yaml,
    _translate_policy_entry_yaml,
    _translate_prompt_policy_yaml,
    _translate_skills_filter_from_yaml,
    agent_def_to_agent_spec,
    agent_spec_to_agent_def,
)


def _minimal_spec(**overrides: object) -> AgentSpec:
    defaults: dict[str, object] = {
        "spec_version": 1,
        "name": "edge-agent",
        "llm": LLMConfig(model="databricks-gpt-5-mini"),
        "executor": ExecutorSpec(
            type="omnigent",
            model="databricks-gpt-5-mini",
            config={"harness": "openai-agents"},
        ),
    }
    defaults.update(overrides)
    return AgentSpec(**defaults)  # type: ignore[arg-type]


def test_reject_unsupported_concepts_cancellable_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.spec.omnigent._is_cancellable_function_path",
        lambda _path: True,
    )
    spec = _minimal_spec(
        local_tools=[
            LocalToolInfo(
                name="async_job",
                path="pkg.module.runner",
                language="python",
            )
        ]
    )
    with pytest.raises(OmnigentError, match=r"cancellable_function tool"):
        _reject_unsupported_concepts(spec)


def test_uc_function_tool_translates_forward() -> None:
    spec = _minimal_spec(
        local_tools=[
            LocalToolInfo(
                name="uc_search",
                path=None,
                language="python",
                runtime=ToolRuntime.UC_FUNCTION,
                catalog_path="main.tools.search",
                warehouse_id="wh-123",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        ]
    )
    agent_def = agent_spec_to_agent_def(spec)
    tool = agent_def.tools["uc_search"]
    assert isinstance(tool, FunctionTool)
    assert tool.catalog_path == "main.tools.search"
    assert tool.warehouse_id == "wh-123"


def test_mcp_server_to_mcp_tool_rejects_invalid_shapes() -> None:
    with pytest.raises(OmnigentError, match=r"transport='stdio' but command is None"):
        _mcp_server_to_mcp_tool(
            MCPServerConfig(name="broken-stdio", transport="stdio", command=None)
        )
    with pytest.raises(OmnigentError, match=r"transport='http' but url is None"):
        _mcp_server_to_mcp_tool(MCPServerConfig(name="broken-http", transport="http", url=None))


def test_resolve_dotted_attr_validation_errors() -> None:
    with pytest.raises(OmnigentError, match=r"must be a dotted path"):
        _resolve_dotted_attr("nodots", "t")
    with pytest.raises(OmnigentError, match=r"has no attribute"):
        _resolve_dotted_attr("tests.spec.test_omnigent_edges.MISSING_ATTR", "t")


def test_resolve_dotted_callable_rejects_non_callable() -> None:
    with pytest.raises(OmnigentError, match=r"non-callable"):
        _resolve_dotted_callable("tests.spec.test_omnigent_translator.NOT_A_FUNCTION", "t")


def test_resolve_dotted_callable_returns_callable() -> None:
    resolved = _resolve_dotted_callable(
        "tests.spec.test_omnigent_translator.sample_tool_callable",
        "sample",
    )
    assert callable(resolved)
    assert resolved("ping") == "ok"


def test_translate_labels_yaml_preserves_unknown_monotonic() -> None:
    out = _translate_labels_yaml(
        None,
        {"risk": {"values": ["low", "high"], "monotonic": "weird"}},
    )
    assert out["risk"]["monotonic"] == "weird"


def test_translate_policies_yaml_passes_through_malformed_entries() -> None:
    out = _translate_policies_yaml({"audit": "not-a-dict"})
    assert out["audit"] == "not-a-dict"


def test_translate_policy_entry_yaml_rejects_unknown_type() -> None:
    with pytest.raises(OmnigentError, match=r"unknown type"):
        _translate_policy_entry_yaml("audit", {"type": "mystery"})


def test_translate_policy_entry_yaml_dispatches_prompt_policies() -> None:
    out = _translate_policy_entry_yaml(
        "block_canada",
        {"type": "prompt", "prompt": "Deny Canada.", "on": ["request"]},
        parent_profile="DEFAULT",
    )
    assert out["type"] == "function"
    assert "prompt_policy" in out["function"]["path"]


def test_translate_function_policy_yaml_native_and_early_return() -> None:
    native = _translate_function_policy_yaml(
        {
            "type": "function",
            "function": "myorg.policies.audit",
            "on": ["request"],
        }
    )
    assert native["function"] == "myorg.policies.audit"
    assert native["on"] == ["request"]

    incomplete = _translate_function_policy_yaml({"type": "function"})
    assert "function" not in incomplete


def test_translate_prompt_policy_yaml_maps_to_builtin_factory() -> None:
    out = _translate_prompt_policy_yaml(
        {"type": "prompt", "prompt": "Deny Canada.", "on": ["request"]}
    )
    assert out["type"] == "function"
    assert out["function"]["path"] == "omnigent.policies.builtins.prompt.prompt_policy"
    assert out["function"]["arguments"]["prompt"] == "Deny Canada."


def test_resolve_profile_to_connection_reads_databrickscfg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._read_databrickscfg",
        lambda profile: (
            SimpleNamespace(host="https://dbc.example.com", token="tok")
            if profile == "DEFAULT"
            else None
        ),
    )
    resolved = _resolve_profile_to_connection("DEFAULT")
    assert resolved == {
        "base_url": "https://dbc.example.com/serving-endpoints",
        "api_key": "tok",
    }
    assert _resolve_profile_to_connection("MISSING") is None


def test_agent_def_to_agent_spec_syncs_llm_connection_to_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.spec.omnigent._translate_llm_from_def",
        lambda *args, **kwargs: LLMConfig(
            model="databricks-gpt-5-mini",
            connection={"api_key": "sk-test"},
        ),
    )
    agent_def = AgentDef(
        name="conn-sync",
        prompt="p",
        executor=OmniExecutorSpec(model="databricks-gpt-5-mini", harness="openai-agents"),
    )
    spec = agent_def_to_agent_spec(agent_def)
    assert spec.executor.connection == {"api_key": "sk-test"}


@pytest.mark.parametrize(
    ("raw_yaml", "match"),
    [
        ({"skills": "bogus"}, r'must be "all", "none", or a list'),
        ({"skills": [1, 2]}, r"list items must be strings"),
        ({"skills": 42}, r"must be \"all\", \"none\", or a list"),
    ],
)
def test_translate_skills_filter_from_yaml_rejects_invalid(
    raw_yaml: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(OmnigentError, match=match):
        _translate_skills_filter_from_yaml(raw_yaml)


def test_translate_skills_filter_from_yaml_normalizes_shapes() -> None:
    assert _translate_skills_filter_from_yaml({"skills": "none"}) == "none"
    assert _translate_skills_filter_from_yaml({"skills": []}) == "none"
    assert _translate_skills_filter_from_yaml({"skills": ["a", "b"]}) == ["a", "b"]


def test_agent_tool_to_sub_spec_recurses_nested_tools() -> None:
    def nested_lookup(query: str) -> str:
        return query

    parent_tool = AgentTool(
        name="lead",
        prompt="Lead researcher",
        executor=OmniExecutorSpec(model="databricks-claude-sonnet-4", harness="claude-sdk"),
        tools={
            "fact_checker": AgentTool(
                name="fact_checker",
                prompt="Check facts",
                executor=OmniExecutorSpec(harness="claude-sdk"),
            ),
            "lookup": FunctionTool(name="lookup", callable=nested_lookup),
        },
    )
    sub_spec = _agent_tool_to_sub_spec(
        "lead",
        parent_tool,
        parent_harness="openai-agents",
        parent_profile="DEFAULT",
    )
    assert len(sub_spec.sub_agents) == 1
    assert sub_spec.sub_agents[0].name == "fact_checker"
    assert len(sub_spec.local_tools) == 1
    assert sub_spec.local_tools[0].name == "lookup"


def test_resolve_inline_agent_tool_os_env_non_inherit_string_returns_none() -> None:
    tool = AgentTool(name="w", prompt="", os_env="custom")
    assert _resolve_inline_agent_tool_os_env(tool, OSEnvSpec(type="caller_process")) is None


def test_translate_mcp_tool_from_def_requires_transport_fields() -> None:
    with pytest.raises(OmnigentError, match=r"has neither 'url' nor 'command'"):
        _translate_mcp_tool_from_def("ghost", MCPTool())


def test_translate_function_tool_from_def_rejects_non_function_tool() -> None:
    with pytest.raises(OmnigentError, match=r"expected FunctionTool"):
        _translate_function_tool_from_def("bad", MCPTool(command="npx"))


def test_recover_callable_path_errors() -> None:
    with pytest.raises(OmnigentError, match=r"no resolved callable"):
        _recover_callable_path("sleep", FunctionTool(name="sleep", callable=None))

    class _NoModule:
        __qualname__ = "fn"

    with pytest.raises(OmnigentError, match=r"no recoverable dotted path"):
        _recover_callable_path("sleep", FunctionTool(name="sleep", callable=_NoModule()))


def test_is_cancellable_function_path_hook_returns_false_by_default() -> None:
    assert _is_cancellable_function_path("any.path.here") is False