"""Edge-case coverage for :mod:`bytedesk_omnigent.policies.github`."""

from __future__ import annotations

import pytest

from bytedesk_omnigent.policies.github import (
    _classify_gh,
    _classify_git,
    _classify_mcp_tool,
    _extract_repos_from_args,
    _flag_value,
    _normalize_branch,
    _normalize_repo,
    _repo_from_url,
    github_policy,
)
from omnigent.policies.schema import PolicyEvent
from tests.bytedesk_omnigent.policies.test_github import _sh
from tests.policies.builtins.helpers import tool_call_event as tc


def test_normalize_repo_empty_and_unparseable() -> None:
    """Blank and non-repo strings normalize to empty."""
    assert _normalize_repo("") == ""
    assert _normalize_repo("not-a-repo") == ""


def test_normalize_branch_strips_ref_and_fork_prefixes() -> None:
    """Branch refs and cross-fork prefixes are reduced to bare names."""
    assert _normalize_branch("") == ""
    assert _normalize_branch("refs/heads/main") == "main"
    assert _normalize_branch("some-fork:feature") == "feature"


def test_repo_from_url_requires_github_host() -> None:
    """Bare owner/repo without a github.com host is not extracted from URLs."""
    assert _repo_from_url("octo/hello") == ""


def test_extract_repos_from_owner_repo_and_url_args() -> None:
    """Owner+repo, slash-separated keys, and embedded URLs are all collected."""
    repos = _extract_repos_from_args(
        {
            "owner": "Octo",
            "repo": "Hello",
            "repository": "https://github.com/other/repo",
            "body": "see https://github.com/octo/hello/issues/1",
        }
    )
    assert repos == {"octo/hello", "other/repo"}


def test_classify_mcp_tool_verb_heuristic_write() -> None:
    """Unknown prefixed tools with write verb prefixes classify as write."""
    assert _classify_mcp_tool("create_custom_thing", use_verb_heuristic=True) == "write"


def test_flag_value_reads_equals_form() -> None:
    """``--repo=owner/name`` spellings are parsed."""
    assert _flag_value(["--repo=octo/hello"], frozenset({"--repo", "-R"})) == "octo/hello"


def test_classify_git_and_gh_require_subcommand() -> None:
    """Bare ``git`` / ``gh`` with no subcommand produce no shell op."""
    assert _classify_git(["git"]) is None
    assert _classify_gh(["gh"]) is None


def test_malformed_tool_call_event_shapes_abstain() -> None:
    """Malformed tool_call events abstain without crashing."""
    policy = github_policy()
    for event in (
        {"type": "request", "data": "git push"},
        {"type": "tool_call", "data": None},
        {"type": "tool_call", "data": {"name": 42, "arguments": {}}},
        {"type": "tool_call", "data": {"name": "sys_os_shell", "arguments": {"command": "  "}}},
    ):
        assert policy(event) is None


def test_env_only_shell_segment_abstains() -> None:
    """Segments that reduce to only env assignments abstain."""
    policy = github_policy()
    assert policy(_sh("GITHUB_TOKEN=secret")) is None


def test_excessive_shell_nesting_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Beyond ``MAX_SHELL_NESTING`` shell unwrapping stops."""
    monkeypatch.setattr("bytedesk_omnigent.policies.github.MAX_SHELL_NESTING", 0)
    policy = github_policy()
    assert policy(_sh('bash -c "git push origin main"')) is None


def test_verb_heuristic_write_gates_unknown_prefixed_tool() -> None:
    """Unknown ``create_*`` GitHub MCP tools are treated as writes."""
    policy = github_policy(write_repos=["octo/hello"])
    result = policy(tc("mcp__github__create_custom_widget", {"owner": "octo", "repo": "secret"}))
    assert result is not None
    assert result["result"] == "DENY"