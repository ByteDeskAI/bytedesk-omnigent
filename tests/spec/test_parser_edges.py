"""Edge-case coverage for :mod:`omnigent.spec.parser` helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.types import Phase, PhaseSelector
from omnigent.spec.parser import (
    _discover_skills,
    _discover_sub_agents,
    _parse_action_list,
    _parse_builtin_tools,
    _parse_compaction,
    _parse_credential_proxy,
    _parse_egress_rules,
    _parse_executor,
    _parse_executor_auth,
    _parse_function_ref,
    _parse_guardrails,
    _parse_interaction,
    _parse_on,
    _parse_on_entry,
    _parse_os_env_sandbox,
    _parse_sandbox_config,
    _parse_skill,
    _parse_terminals,
    _parse_writable_labels,
    _read_contained_file,
    _resolve_instructions,
    _validate_supervisor_tool_entry,
    discover_host_skills,
    parse,
    parse_default_policies,
    parse_server_llm,
)
from tests.spec.test_parser import _write_supervisor_config


def _write_config(root: Path, config: dict[str, object]) -> None:
    (root / "config.yaml").write_text(yaml.dump(config))


# ── Small helper parsers ────────────────────────────────────────


def test_parse_interaction_non_dict_modalities_uses_defaults() -> None:
    result = _parse_interaction({"modalities": ["text"]})
    assert result.modalities.input == ["text"]
    assert result.modalities.output == ["text"]


def test_parse_sandbox_config_non_mapping_returns_defaults() -> None:
    assert _parse_sandbox_config("not-a-dict").docker_image is None


def test_parse_sandbox_config_mapping_parses_docker_image() -> None:
    cfg = _parse_sandbox_config({"docker_image": "python:3.12-slim"})
    assert cfg.docker_image == "python:3.12-slim"


def test_parse_builtin_tools_rejects_invalid_entry_type() -> None:
    with pytest.raises(OmnigentError, match=r"must be strings or dicts"):
        _parse_builtin_tools(["ok", 42])  # type: ignore[list-item]


def test_parse_executor_connection_block_expands_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXEC_KEY", "secret")
    spec = _parse_executor(
        {
            "type": "omnigent",
            "config": {"harness": "claude-sdk"},
            "connection": {"api_key": "${EXEC_KEY}"},
        }
    )
    assert spec.connection == {"api_key": "secret"}


def test_parse_executor_auth_non_mapping_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"executor.auth must be a mapping"):
        _parse_executor_auth({"auth": "bad"})


# ── Supervisor tool validation ──────────────────────────────────


def test_validate_supervisor_tool_entry_rejects_non_mapping() -> None:
    with pytest.raises(OmnigentError, match=r"must be a YAML mapping"):
        _validate_supervisor_tool_entry(0, "not-a-dict")


def test_validate_supervisor_tool_entry_missing_type(tmp_path: Path) -> None:
    _write_supervisor_config(tmp_path, tools=[{"genie_space": {"id": "x", "description": "d"}}])
    with pytest.raises(OmnigentError, match=r"missing required key 'type'"):
        parse(tmp_path)


def test_validate_supervisor_tool_entry_missing_nested_mapping(tmp_path: Path) -> None:
    _write_supervisor_config(tmp_path, tools=[{"type": "genie_space", "id": "x"}])
    with pytest.raises(OmnigentError, match=r"must include a nested 'genie_space' mapping"):
        parse(tmp_path)


# ── Terminals block ─────────────────────────────────────────────


def test_parse_terminals_rejects_non_mapping_root() -> None:
    with pytest.raises(OmnigentError, match=r"terminals must be a YAML mapping"):
        _parse_terminals(["bad"])


def test_parse_terminals_rejects_non_mapping_entry() -> None:
    with pytest.raises(OmnigentError, match=r"terminals.shell must be a YAML mapping"):
        _parse_terminals({"shell": "bad"})


def test_parse_terminals_rejects_bad_args_type() -> None:
    with pytest.raises(OmnigentError, match=r"terminals.shell.args must be a list"):
        _parse_terminals({"shell": {"command": "bash", "args": "bad"}})


def test_parse_terminals_rejects_bad_env_type() -> None:
    with pytest.raises(OmnigentError, match=r"terminals.shell.env must be a mapping"):
        _parse_terminals({"shell": {"command": "bash", "env": "bad"}})


def test_parse_terminals_parses_full_entry() -> None:
    terminals = _parse_terminals(
        {
            "shell": {
                "command": "bash",
                "args": ["-l"],
                "env": {"FOO": "bar"},
                "allow_cwd_override": True,
                "scrollback": 500,
            }
        }
    )
    assert terminals is not None
    assert terminals["shell"].command == "bash"
    assert terminals["shell"].args == ["-l"]
    assert terminals["shell"].env == {"FOO": "bar"}
    assert terminals["shell"].allow_cwd_override is True
    assert terminals["shell"].scrollback == 500


# ── Sandbox default type + credential proxy extras ────────────────


def test_parse_os_env_sandbox_default_type_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.inner.sandbox._default_sandbox_for_platform",
        lambda: MagicMock(type="linux_bwrap"),
    )
    spec = _parse_os_env_sandbox({})
    assert spec is not None
    assert spec.type == "linux_bwrap"


def test_parse_egress_rules_none_and_empty() -> None:
    assert _parse_egress_rules(None) is None
    assert _parse_egress_rules([]) is None


def test_parse_egress_rules_validates_entries() -> None:
    rules = ["GET api.github.com/repos/**", "* pypi.org/**"]
    assert _parse_egress_rules(rules) == rules
    with pytest.raises(OmnigentError, match=r"must be a list"):
        _parse_egress_rules("GET api.github.com/**")
    with pytest.raises(OmnigentError, match=r"must be strings"):
        _parse_egress_rules([123])
    with pytest.raises(OmnigentError, match=r"is invalid"):
        _parse_egress_rules(["BADMETHOD api.github.com/**"])


def test_parse_os_env_sandbox_egress_rules_rejected_for_none_backend() -> None:
    with pytest.raises(OmnigentError, match=r"linux_bwrap.*darwin_seatbelt"):
        _parse_os_env_sandbox({"type": "none", "egress_rules": ["GET api.github.com/**"]})


def test_parse_os_env_sandbox_egress_allow_private_must_be_bool() -> None:
    with pytest.raises(OmnigentError, match=r"egress_allow_private_destinations must be a boolean"):
        _parse_os_env_sandbox(
            {"type": "linux_bwrap", "egress_allow_private_destinations": "true"}
        )


@pytest.mark.parametrize(
    "entry,match",
    [
        (
            {
                "type": "https_basic",
                "targets": ["h.example.com"],
                "source": {"env": "X"},
                "username": "",
            },
            r"username must be a non-empty string",
        ),
        (
            {
                "type": "https_bearer",
                "targets": [],
                "source": {"env": "X"},
            },
            r"targets must be a non-empty list",
        ),
        (
            {
                "type": "gh_basic",
                "target": "github.com",
                "targets": ["api.github.com"],
                "source": {"env": "X"},
            },
            r"gh_basic accepts at most one of 'target' or 'targets'",
        ),
        (
            {
                "type": "git_https",
                "target": "git.example.com",
                "source": {"env": "X"},
                "env": "TOKEN",
            },
            r"git_https does not accept an 'env' injection shim",
        ),
        (
            {
                "type": "https_bearer",
                "target": "h.example.com",
                "source": {"env": "X"},
                "username": "svc",
            },
            r"https_bearer does not accept a 'username'",
        ),
        (
            {
                "type": "https_bearer",
                "target": "",
                "source": {"env": "X"},
            },
            r"must be a non-empty string",
        ),
        (
            {"type": "https_bearer", "target": "h.example.com", "source": {"env": " "}},
            r"source 'env' must be a POSIX",
        ),
        (
            {"type": "https_bearer", "target": "h.example.com", "source": {"file": "  "}},
            r"source 'file' must be a non-empty path",
        ),
        (
            {"type": "https_bearer", "target": "h.example.com", "source": {"command": "  "}},
            r"source 'command' must be a non-empty command",
        ),
    ],
)
def test_parse_credential_proxy_validation_edges(entry: dict[str, object], match: str) -> None:
    config = {
        "type": "linux_bwrap",
        "egress_rules": ["* h.example.com/**"],
        "credential_proxy": [entry],
    }
    with pytest.raises(OmnigentError, match=match):
        _parse_os_env_sandbox(config)


def test_parse_credential_proxy_list_type_and_empty(tmp_path: Path) -> None:
    with pytest.raises(OmnigentError, match=r"credential_proxy must be a list"):
        _parse_credential_proxy("bad")

    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "empty-proxy",
            "os_env": {
                "type": "caller_process",
                "sandbox": {"type": "linux_bwrap", "credential_proxy": []},
            },
        },
    )
    assert parse(tmp_path).os_env.sandbox.credential_proxy is None


def test_parse_credential_proxy_gh_basic_explicit_targets(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "gh-targets",
            "os_env": {
                "type": "caller_process",
                "sandbox": {
                    "type": "linux_bwrap",
                    "egress_rules": ["* github.com/**", "* api.github.com/**"],
                    "credential_proxy": [
                        {
                            "type": "gh_basic",
                            "targets": ["github.com"],
                            "source": {"command": "gh auth token"},
                        }
                    ],
                },
            },
        },
    )
    spec = parse(tmp_path)
    proxy = spec.os_env.sandbox.credential_proxy
    assert proxy is not None
    assert len(proxy.entries) == 1
    assert proxy.entries[0].source.kind == "command"


def test_parse_credential_proxy_returns_none_when_normalize_produces_no_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.spec.parser._normalize_https_bearer",
        lambda *args, **kwargs: [],
    )
    assert (
        _parse_credential_proxy(
            [{"type": "https_bearer", "target": "h.example.com", "source": {"env": "TOKEN"}}]
        )
        is None
    )


# ── Compaction + instruction file helpers ─────────────────────────


def test_parse_compaction_block() -> None:
    cfg = _parse_compaction({"trigger_threshold": 0.5, "recent_window": 3})
    assert cfg is not None
    assert cfg.trigger_threshold == 0.5
    assert cfg.recent_window == 3


def test_read_contained_file_oserror_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "prompt.md"
    target.write_text("hello")
    monkeypatch.setattr(Path, "read_text", MagicMock(side_effect=OSError("nope")))
    assert _read_contained_file(tmp_path, "prompt.md") is None


def test_resolve_instructions_oserror_on_default_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "AGENTS.md").write_text("from agents")
    monkeypatch.setattr(Path, "is_file", MagicMock(side_effect=OSError("nope")))
    assert _resolve_instructions(tmp_path, None) is None


# ── Host skills discovery ───────────────────────────────────────


def test_discover_host_skills_none_filter_returns_empty(tmp_path: Path) -> None:
    assert discover_host_skills(tmp_path, "none") == []


def test_discover_host_skills_named_subset_filters(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".agents" / "skills"
    kept = skills_dir / "keep"
    kept.mkdir(parents=True)
    (kept / "SKILL.md").write_text("---\nname: keep\ndescription: keep\n---\n")
    skipped = skills_dir / "skip"
    skipped.mkdir(parents=True)
    (skipped / "SKILL.md").write_text("---\nname: skip\ndescription: skip\n---\n")
    result = discover_host_skills(tmp_path, ["keep"])
    assert [s.name for s in result] == ["keep"]


def test_discover_host_skills_skips_duplicate_names(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude" / "skills" / "from-claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "SKILL.md").write_text(
        "---\nname: dup\ndescription: first\n---\nfrom claude"
    )
    agents_dir = tmp_path / ".agents" / "skills" / "from-agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "SKILL.md").write_text(
        "---\nname: dup\ndescription: second\n---\nfrom agents"
    )
    result = discover_host_skills(tmp_path, ["dup"])
    assert len(result) == 1
    assert result[0].content.strip() == "from claude"


def test_discover_skills_skips_non_directory_entries(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "not-a-dir.txt").write_text("x")
    good = skills_dir / "good"
    good.mkdir()
    (good / "SKILL.md").write_text("---\nname: good\ndescription: good\n---\n")
    assert [s.name for s in _discover_skills(skills_dir)] == ["good"]


def test_discover_skills_skips_dirs_without_skill_md(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "empty-dir").mkdir()
    good = skills_dir / "good"
    good.mkdir()
    (good / "SKILL.md").write_text("---\nname: good\ndescription: good\n---\n")
    assert [s.name for s in _discover_skills(skills_dir)] == ["good"]


def test_parse_skill_frontmatter_must_be_mapping(tmp_path: Path) -> None:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\n- list\n---\nbody")
    with pytest.raises(OmnigentError, match=r"frontmatter must be a YAML mapping"):
        _parse_skill(skill_md)


# ── Inline / file MCP + sub-agents ──────────────────────────────


def test_parse_inline_mcp_databricks_auth_requires_profile(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "mcp-db",
            "tools": {
                "db": {
                    "type": "mcp",
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "auth": {"type": "databricks"},
                }
            },
        },
    )
    with pytest.raises(OmnigentError, match=r"requires a 'profile' field"):
        parse(tmp_path)


def test_parse_inline_mcp_databricks_auth_happy_path(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "mcp-db-ok",
            "tools": {
                "db": {
                    "type": "mcp",
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "auth": {"type": "databricks", "profile": "DEFAULT"},
                }
            },
        },
    )
    spec = parse(tmp_path)
    assert len(spec.mcp_servers) == 1
    assert spec.mcp_servers[0].databricks_profile == "DEFAULT"
    assert spec.mcp_servers[0].oauth is None


def test_parse_inline_mcp_oauth_requires_token_url_and_client_id(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "mcp-oauth",
            "tools": {
                "oauth": {
                    "type": "mcp",
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "auth": {"type": "oauth", "client_id": "cid"},
                }
            },
        },
    )
    with pytest.raises(OmnigentError, match=r"requires a 'token_url' field"):
        parse(tmp_path)


def test_parse_inline_mcp_oauth_happy_path(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "mcp-oauth-ok",
            "tools": {
                "oauth": {
                    "type": "mcp",
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "auth": {
                        "type": "oauth",
                        "token_url": "https://auth.example.com/token",
                        "client_id": "cid",
                        "client_secret": "sec",
                        "scopes": "read write",
                        "resource": "https://api.example.com",
                    },
                }
            },
        },
    )
    spec = parse(tmp_path)
    assert len(spec.mcp_servers) == 1
    oauth = spec.mcp_servers[0].oauth
    assert oauth is not None
    assert oauth.token_url == "https://auth.example.com/token"
    assert oauth.client_id == "cid"
    assert oauth.client_secret == "sec"
    assert oauth.scopes == ["read write"]
    assert oauth.resource == "https://api.example.com"
    assert spec.mcp_servers[0].databricks_profile is None


def test_parse_tools_ignores_non_mapping_non_mcp_entries(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "tools-mix",
            "tools": {"ignored": "plain-string"},
        },
    )
    assert parse(tmp_path).mcp_servers == []


def test_discover_mcp_file_non_mapping_rejected(tmp_path: Path) -> None:
    mcp_dir = tmp_path / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "bad.yaml").write_text("- not-a-mapping")
    _write_config(tmp_path, {"spec_version": 1, "name": "mcp-bad"})
    with pytest.raises(OmnigentError, match=r"MCP config must be a YAML mapping"):
        parse(tmp_path)


def test_discover_mcp_stdio_bad_args_and_env(tmp_path: Path) -> None:
    mcp_dir = tmp_path / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "stdio.yaml").write_text(
        yaml.dump(
            {
                "name": "stdio",
                "transport": "stdio",
                "command": "echo",
                "args": "bad",
            }
        )
    )
    _write_config(tmp_path, {"spec_version": 1, "name": "stdio-bad-args"})
    with pytest.raises(OmnigentError, match=r"'args' must be a list"):
        parse(tmp_path)

    (mcp_dir / "stdio.yaml").write_text(
        yaml.dump(
            {
                "name": "stdio",
                "transport": "stdio",
                "command": "echo",
                "env": "bad",
            }
        )
    )
    with pytest.raises(OmnigentError, match=r"'env' must be a mapping"):
        parse(tmp_path)


def test_discover_sub_agents_skips_non_dirs_and_missing_config(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "file.txt").write_text("x")
    empty = agents_dir / "empty"
    empty.mkdir()
    child = agents_dir / "child"
    child.mkdir()
    (child / "config.yaml").write_text(yaml.dump({"spec_version": 1, "name": "child"}))
    assert [a.name for a in _discover_sub_agents(agents_dir, expand_env=False)] == ["child"]


# ── Guardrails / policies edges ─────────────────────────────────


def _guardrails_yaml(body: str) -> dict[str, object]:
    from tests.spec.test_policy_parser import _yaml

    return _parse_guardrails(_yaml(body))


def test_parse_guardrails_labels_non_mapping_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"guardrails.labels: must be a mapping"):
        _guardrails_yaml("labels: [bad]")


def test_parse_guardrails_label_scalar_shorthand() -> None:
    spec = _guardrails_yaml("labels:\n  count: 1")
    assert spec is not None and spec.labels is not None
    assert spec.labels["count"].initial == "1"


def test_parse_guardrails_label_invalid_entry_type_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"label 'x' must be a string or mapping"):
        _guardrails_yaml("labels:\n  x: [bad]")


def test_parse_guardrails_policies_non_mapping_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"guardrails.policies: must be a mapping"):
        _guardrails_yaml("policies: [bad]")


def test_parse_guardrails_policy_data_non_mapping_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"policy 'p'.*must be a mapping"):
        _guardrails_yaml("policies:\n  p: bad")


def test_parse_guardrails_prompt_policy_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"type 'prompt' is no longer supported"):
        _guardrails_yaml(
            """
policies:
  old:
    type: prompt
    on: [response]
"""
        )


def test_parse_function_policy_config_must_be_dict() -> None:
    with pytest.raises(OmnigentError, match=r"'config' must be a dict"):
        _guardrails_yaml(
            """
policies:
  p:
    type: function
    on: [request]
    function: myorg.p.check
    config: bad
"""
        )


def test_parse_policy_on_helpers_validate_directly() -> None:
    with pytest.raises(OmnigentError, match=r"`on:` must be a list"):
        _parse_on("request", policy_name="p")
    with pytest.raises(OmnigentError, match=r"`on:` must contain at least one"):
        _parse_on([], policy_name="p")
    with pytest.raises(OmnigentError, match=r"`on:` entries must be strings"):
        _parse_on_entry(1, policy_name="p")
    with pytest.raises(OmnigentError, match=r"empty tool name"):
        _parse_on_entry("tool_call:", policy_name="p")
    with pytest.raises(OmnigentError, match=r"cannot be narrowed by tool name"):
        _parse_on_entry("request:web_search", policy_name="p")
    with pytest.raises(OmnigentError, match=r"unknown phase"):
        _parse_on_entry("bogus", policy_name="p")


def test_parse_on_entry_tool_call_with_tool_name() -> None:
    selector = _parse_on_entry("tool_call:web_search", policy_name="p")
    assert selector == PhaseSelector(phase=Phase.TOOL_CALL, tool_name="web_search")


def test_parse_writable_labels_none_returns_none() -> None:
    assert _parse_writable_labels(None, policy_name="p") is None


def test_parse_policy_action_helpers_validate_directly() -> None:
    with pytest.raises(OmnigentError, match=r"`action:` must be a string or"):
        _parse_action_list({"bad": 1}, policy_name="p")
    with pytest.raises(OmnigentError, match=r"`action:` list must be non-empty"):
        _parse_action_list([], policy_name="p")
    with pytest.raises(OmnigentError, match=r"invalid action 'bogus'"):
        _parse_action_list(["bogus"], policy_name="p")
    assert _parse_action_list("allow", policy_name="p")


def test_parse_policy_set_labels_must_be_list() -> None:
    with pytest.raises(OmnigentError, match=r"`set_labels:` must be a list"):
        _parse_writable_labels("bad", policy_name="p")


def test_parse_policy_function_ref_validation() -> None:
    with pytest.raises(OmnigentError, match=r"`function.path` must be a non-empty"):
        _parse_function_ref({"path": ""}, policy_name="p")
    with pytest.raises(OmnigentError, match=r"`function:` path must be non-empty"):
        _parse_function_ref("", policy_name="p")
    with pytest.raises(OmnigentError, match=r"must be a dotted-path string or a dict"):
        _parse_function_ref(1, policy_name="p")


def test_parse_policy_ask_timeout_non_integer_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"`ask_timeout` must be an integer"):
        _guardrails_yaml(
            """
policies:
  p:
    type: function
    on: [request]
    function: myorg.p.check
    ask_timeout: soon
"""
        )


def test_parse_default_policies_and_server_llm_entrypoints() -> None:
    assert parse_default_policies(None) == []
    assert parse_default_policies({}) == []
    policies = parse_default_policies(
        {
            "audit": {
                "type": "function",
                "on": ["request"],
                "function": "myorg.policies.audit",
            }
        }
    )
    assert len(policies) == 1
    assert parse_server_llm({"model": "openai/gpt-4o-mini"}, expand_env=False) is not None


# ── Top-level parse integration for remaining branches ──────────


def test_parse_interaction_modalities_list_via_config(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"spec_version": 1, "name": "x", "interaction": {"modalities": ["text"]}},
    )
    spec = parse(tmp_path)
    assert spec.interaction.modalities.input == ["text"]


def test_parse_tools_builtin_invalid_entry_via_config(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"spec_version": 1, "name": "x", "tools": {"builtins": ["ok", 1]}},
    )
    with pytest.raises(OmnigentError, match=r"must be strings or dicts"):
        parse(tmp_path)


def test_parse_executor_auth_string_via_config(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "x",
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}, "auth": "bad"},
        },
    )
    with pytest.raises(OmnigentError, match=r"executor.auth must be a mapping"):
        parse(tmp_path)


def test_parse_compaction_via_config(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "x",
            "compaction": {"trigger_threshold": 0.7, "recent_window": 2},
        },
    )
    spec = parse(tmp_path)
    assert spec.compaction is not None
    assert spec.compaction.trigger_threshold == 0.7