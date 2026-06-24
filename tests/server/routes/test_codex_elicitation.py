"""Tests for the Codex elicitation protocol adapters.

These are pure-function tests — no HTTP or runtime needed.
"""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.server.routes._codex_elicitation import (
    _codex_apply_patch_approval_response,
    _codex_available_execpolicy_amendment,
    _codex_command_approval_response,
    _codex_command_preview,
    _codex_file_change_approval_response,
    _codex_mcp_elicitation_response,
    _codex_permissions_approval_response,
    _codex_request_user_input_response,
    _decision_execpolicy_amendment,
    _execpolicy_amendment,
    _json_preview,
    _result_execpolicy_amendment,
    _string_list_answer,
    _structured_codex_request_user_input,
    parse_codex_elicitation_request,
)
from omnigent.server.schemas import ElicitationResult

# ── parse_codex_elicitation_request ──────────────────────────────────


class TestParseCodexElicitationRequest:
    """Tests for the top-level request parser."""

    def test_missing_method_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty method"):
            parse_codex_elicitation_request({"id": 1, "params": {}})

    def test_empty_method_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty method"):
            parse_codex_elicitation_request({"id": 1, "method": "", "params": {}})

    def test_non_dict_params_raises(self) -> None:
        with pytest.raises(OmnigentError, match="params must be an object"):
            parse_codex_elicitation_request(
                {"id": 1, "method": "mcpServer/elicitation/request", "params": "bad"}
            )

    def test_missing_id_raises(self) -> None:
        with pytest.raises(OmnigentError, match="string or integer id"):
            parse_codex_elicitation_request(
                {"method": "mcpServer/elicitation/request", "params": {}}
            )

    def test_unsupported_method_raises(self) -> None:
        with pytest.raises(OmnigentError, match="Unsupported"):
            parse_codex_elicitation_request({"id": 1, "method": "unknown/method", "params": {}})

    def test_valid_mcp_form_request(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 1,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "mode": "form",
                    "message": "Need input",
                    "requestedSchema": {"type": "object"},
                },
            }
        )
        assert req.method == "mcpServer/elicitation/request"
        assert req.params.mode == "form"

    def test_valid_command_approval(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 2,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "npm test"},
            }
        )
        assert req.method == "item/commandExecution/requestApproval"

    def test_build_response_delegates_to_adapter(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 3,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "mode": "form",
                    "message": "Confirm",
                    "requestedSchema": {"type": "object"},
                },
            }
        )
        result = ElicitationResult(action="accept", content={"ok": True})
        payload = req.build_response(result)
        assert payload == {
            "action": "accept",
            "content": {"ok": True},
            "_meta": None,
        }


# ── _structured_codex_request_user_input ─────────────────────────────


class TestStructuredCodexRequestUserInput:
    """Tests for Codex question normalization."""

    def test_missing_questions_returns_none(self) -> None:
        assert _structured_codex_request_user_input({}) is None

    def test_empty_questions_returns_none(self) -> None:
        assert _structured_codex_request_user_input({"questions": []}) is None

    def test_non_list_questions_returns_none(self) -> None:
        assert _structured_codex_request_user_input({"questions": "bad"}) is None

    def test_skips_malformed_entries(self) -> None:
        assert _structured_codex_request_user_input({"questions": ["bad", 1]}) is None

    def test_builds_full_question_shape(self) -> None:
        payload = _structured_codex_request_user_input(
            {
                "questions": [
                    {
                        "id": "framework",
                        "question": "Pick one",
                        "header": "Stack",
                        "isOther": True,
                        "isSecret": False,
                        "options": [
                            {"label": "React", "description": "UI lib"},
                            {"label": "", "description": "skip"},
                            42,
                        ],
                    },
                    {"id": "", "question": "bad"},
                    {"id": "q2", "question": ""},
                ]
            }
        )
        assert payload == {
            "questions": [
                {
                    "id": "framework",
                    "question": "Pick one",
                    "options": [{"label": "React", "description": "UI lib"}],
                    "multiSelect": False,
                    "header": "Stack",
                    "isOther": True,
                    "isSecret": False,
                }
            ]
        }


# ── _string_list_answer ──────────────────────────────────────────────


class TestStringListAnswer:
    """Tests for answer normalization."""

    def test_string_input(self) -> None:
        assert _string_list_answer("React") == ["React"]

    def test_empty_string(self) -> None:
        assert _string_list_answer("") == []

    def test_list_input(self) -> None:
        assert _string_list_answer(["a", "b"]) == ["a", "b"]

    def test_list_with_non_strings(self) -> None:
        assert _string_list_answer(["a", 123, "b"]) == ["a", "b"]

    def test_none_input(self) -> None:
        assert _string_list_answer(None) == []

    def test_numeric_input(self) -> None:
        assert _string_list_answer(42) == ["42"]


# ── _codex_request_user_input_response ───────────────────────────────


class TestCodexRequestUserInputResponse:
    """Tests for requestUserInput response adapter."""

    def test_non_accept_returns_empty_answers(self) -> None:
        result = ElicitationResult(action="decline")
        response = _codex_request_user_input_response(
            result, "item/tool/requestUserInput", {"questions": []}
        )
        assert response == {"answers": {}}

    def test_non_dict_content_returns_empty_answers(self) -> None:
        result = ElicitationResult(action="accept", content=None)
        response = _codex_request_user_input_response(
            result,
            "item/tool/requestUserInput",
            {"questions": [{"id": "q1", "question": "Q?"}]},
        )
        assert response == {"answers": {}}

    def test_maps_answers_by_id_and_question_text(self) -> None:
        result = ElicitationResult(
            action="accept",
            content={"q1": "A", "Fallback?": ["B", ""]},
        )
        response = _codex_request_user_input_response(
            result,
            "item/tool/requestUserInput",
            {
                "questions": [
                    {"id": "q1", "question": "First?"},
                    {"id": "q2", "question": "Fallback?"},
                    "bad",
                    {"id": "", "question": "skip"},
                ]
            },
        )
        assert response == {
            "answers": {
                "q1": {"answers": ["A"]},
                "q2": {"answers": ["B"]},
            }
        }

    def test_non_list_questions_returns_empty(self) -> None:
        result = ElicitationResult(action="accept", content={"q1": "A"})
        response = _codex_request_user_input_response(
            result, "item/tool/requestUserInput", {"questions": "bad"}
        )
        assert response == {"answers": {}}


# ── _codex_mcp_elicitation_response ──────────────────────────────────


class TestCodexMcpElicitationResponse:
    """Tests for MCP elicitation response adapter."""

    def test_accept_includes_content(self) -> None:
        result = ElicitationResult(action="accept", content={"field": "x"})
        assert _codex_mcp_elicitation_response(result, "mcp", {}) == {
            "action": "accept",
            "content": {"field": "x"},
            "_meta": None,
        }

    def test_decline_clears_content(self) -> None:
        result = ElicitationResult(action="decline", content={"field": "x"})
        assert _codex_mcp_elicitation_response(result, "mcp", {}) == {
            "action": "decline",
            "content": None,
            "_meta": None,
        }


# ── execpolicy amendment helpers ─────────────────────────────────────


class TestExecpolicyAmendmentHelpers:
    """Tests for execpolicy amendment extraction and validation."""

    def test_decision_non_dict_returns_none(self) -> None:
        assert _decision_execpolicy_amendment("bad") is None

    def test_decision_without_wrapper_returns_none(self) -> None:
        assert _decision_execpolicy_amendment({"accept": "accept"}) is None

    def test_decision_wrapped_non_object_raises(self) -> None:
        with pytest.raises(OmnigentError, match="acceptWithExecpolicyAmendment"):
            _decision_execpolicy_amendment({"acceptWithExecpolicyAmendment": "bad"})

    def test_decision_extracts_amendment(self) -> None:
        decision = {
            "acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["pytest"]}
        }
        assert _decision_execpolicy_amendment(decision) == ["pytest"]

    def test_available_non_list_returns_none(self) -> None:
        assert _codex_available_execpolicy_amendment({"availableDecisions": "bad"}) is None

    def test_available_scans_decisions(self) -> None:
        params = {
            "availableDecisions": [
                {"accept": "accept"},
                {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["npm", "test"]}},
            ]
        }
        assert _codex_available_execpolicy_amendment(params) == ["npm", "test"]

    def test_available_no_match_returns_none(self) -> None:
        assert _codex_available_execpolicy_amendment({"availableDecisions": []}) is None

    def test_result_non_dict_returns_none(self) -> None:
        assert _result_execpolicy_amendment(None) is None
        assert _result_execpolicy_amendment("bad") is None  # type: ignore[arg-type]

    def test_result_extracts_from_content(self) -> None:
        assert _result_execpolicy_amendment({"execpolicy_amendment": ["pytest"]}) == [
            "pytest"
        ]


class TestExecpolicyAmendment:
    """Tests for execpolicy amendment validation."""

    def test_none_returns_none(self) -> None:
        assert _execpolicy_amendment(None) is None

    def test_valid_list(self) -> None:
        assert _execpolicy_amendment(["pytest", "-v"]) == ["pytest", "-v"]

    def test_empty_list_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty list"):
            _execpolicy_amendment([])

    def test_non_list_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty list"):
            _execpolicy_amendment("pytest")

    def test_list_with_non_strings_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty list"):
            _execpolicy_amendment(["pytest", 42])


# ── command approval response ────────────────────────────────────────


class TestCodexCommandApprovalResponse:
    """Tests for command approval response adapters."""

    def test_v2_accept_decline_cancel(self) -> None:
        for action, expected in [
            ("accept", "accept"),
            ("decline", "decline"),
            ("cancel", "cancel"),
        ]:
            result = ElicitationResult(action=action)  # type: ignore[arg-type]
            payload = _codex_command_approval_response(
                result, "item/commandExecution/requestApproval", {}
            )
            assert payload == {"decision": expected}

    def test_v2_accept_with_matching_execpolicy_amendment(self) -> None:
        result = ElicitationResult(
            action="accept",
            content={"execpolicy_amendment": ["pytest", "-q"]},
        )
        params = {
            "availableDecisions": [
                {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["pytest", "-q"]}}
            ]
        }
        payload = _codex_command_approval_response(
            result, "item/commandExecution/requestApproval", params
        )
        assert payload == {
            "decision": {
                "acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["pytest", "-q"]}
            }
        }

    def test_v2_mismatched_execpolicy_amendment_raises(self) -> None:
        result = ElicitationResult(
            action="accept",
            content={"execpolicy_amendment": ["other"]},
        )
        params = {
            "availableDecisions": [
                {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["pytest"]}}
            ]
        }
        with pytest.raises(OmnigentError, match="did not match"):
            _codex_command_approval_response(
                result, "item/commandExecution/requestApproval", params
            )

    def test_legacy_exec_command_approval_mapping(self) -> None:
        result = ElicitationResult(action="accept")
        payload = _codex_command_approval_response(result, "execCommandApproval", {})
        assert payload == {"decision": "approved"}

    def test_unsupported_method_raises(self) -> None:
        result = ElicitationResult(action="accept")
        with pytest.raises(OmnigentError, match="Unsupported Codex command approval"):
            _codex_command_approval_response(result, "unknown/method", {})


# ── file / patch / permissions responses ─────────────────────────────


class TestOtherApprovalResponses:
    """Tests for file, patch, and permissions response adapters."""

    def test_file_change_decisions(self) -> None:
        for action, expected in [
            ("accept", "accept"),
            ("decline", "decline"),
            ("cancel", "cancel"),
        ]:
            result = ElicitationResult(action=action)  # type: ignore[arg-type]
            assert _codex_file_change_approval_response(result, "item/fileChange", {}) == {
                "decision": expected
            }

    def test_apply_patch_decisions(self) -> None:
        result = ElicitationResult(action="decline")
        assert _codex_apply_patch_approval_response(result, "applyPatchApproval", {}) == {
            "decision": "denied"
        }

    def test_permissions_accept_grants_requested(self) -> None:
        result = ElicitationResult(action="accept")
        params = {"permissions": {"network": True, "fileSystem": "rw", "ignored": 1}}
        assert _codex_permissions_approval_response(
            result, "item/permissions/requestApproval", params
        ) == {"permissions": {"network": True, "fileSystem": "rw"}, "scope": "turn"}

    def test_permissions_decline_returns_empty_grant(self) -> None:
        result = ElicitationResult(action="decline")
        params = {"permissions": {"network": True}}
        assert _codex_permissions_approval_response(
            result, "item/permissions/requestApproval", params
        ) == {"permissions": {}, "scope": "turn"}


# ── MCP elicitation params ───────────────────────────────────────────


class TestCodexMcpElicitationParams:
    """Tests for MCP elicitation param builder via parse."""

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(OmnigentError, match="mode must be"):
            parse_codex_elicitation_request(
                {
                    "id": 1,
                    "method": "mcpServer/elicitation/request",
                    "params": {"mode": "bad", "message": "hi"},
                }
            )

    def test_missing_message_raises(self) -> None:
        with pytest.raises(OmnigentError, match="message must be"):
            parse_codex_elicitation_request(
                {
                    "id": 1,
                    "method": "mcpServer/elicitation/request",
                    "params": {"mode": "form", "requestedSchema": {}},
                }
            )

    def test_form_missing_schema_raises(self) -> None:
        with pytest.raises(OmnigentError, match="requestedSchema"):
            parse_codex_elicitation_request(
                {
                    "id": 1,
                    "method": "mcpServer/elicitation/request",
                    "params": {"mode": "form", "message": "hi"},
                }
            )

    def test_form_includes_optional_extras(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": "req-1",
                "method": "mcpServer/elicitation/request",
                "params": {
                    "mode": "form",
                    "message": "Need input",
                    "requestedSchema": {"type": "object"},
                    "serverName": "srv",
                    "threadId": "thread",
                    "turnId": "turn",
                    "_meta": {"trace": 1},
                },
            }
        )
        assert req.params.server_name == "srv"
        assert req.params.thread_id == "thread"
        assert req.params.turn_id == "turn"
        assert req.params._meta == {"trace": 1}

    def test_url_mode_requires_url(self) -> None:
        with pytest.raises(OmnigentError, match="params.url"):
            parse_codex_elicitation_request(
                {
                    "id": 2,
                    "method": "mcpServer/elicitation/request",
                    "params": {"mode": "url", "message": "Open"},
                }
            )

    def test_url_mode_builds_params(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 2,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "mode": "url",
                    "message": "Open",
                    "url": "https://example.com",
                    "serverName": "",
                },
            }
        )
        assert req.params.mode == "url"
        assert req.params.url == "https://example.com"


# ── tool request user input params ───────────────────────────────────


class TestCodexToolRequestUserInputParams:
    """Tests for requestUserInput param builder."""

    def test_missing_questions_raises(self) -> None:
        with pytest.raises(OmnigentError, match="at least one usable question"):
            parse_codex_elicitation_request(
                {
                    "id": 1,
                    "method": "item/tool/requestUserInput",
                    "params": {"questions": []},
                }
            )

    def test_builds_params_with_extras(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 7,
                "method": "item/tool/requestUserInput",
                "params": {
                    "threadId": "t1",
                    "turnId": "u1",
                    "itemId": "item",
                    "questions": [
                        {"id": "q1", "question": "Pick", "options": [{"label": "A"}]}
                    ],
                },
            }
        )
        assert req.params.phase == "codex_request_user_input"
        assert req.params.thread_id == "t1"
        assert req.params.turn_id == "u1"
        assert req.params.item_id == "item"
        assert req.params.ask_user_question == {
            "questions": [
                {
                    "id": "q1",
                    "question": "Pick",
                    "options": [{"label": "A"}],
                    "multiSelect": False,
                }
            ]
        }


# ── command approval params ──────────────────────────────────────────


class TestCodexCommandApprovalParams:
    """Tests for command approval param builder."""

    def test_populates_all_optional_fields(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 9,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "command": ["npm", "test"],
                    "cwd": "/tmp",
                    "reason": "tests",
                    "conversationId": "conv",
                    "turnId": "turn",
                    "itemId": "item",
                    "callId": "call",
                    "approvalId": "appr",
                    "availableDecisions": [
                        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["npm"]}}
                    ],
                },
            }
        )
        assert req.params.message == "Codex wants to run **npm test**"
        assert req.params.command == "npm test"
        assert req.params.cwd == "/tmp"
        assert req.params.reason == "tests"
        assert req.params.thread_id == "conv"
        assert req.params.turn_id == "turn"
        assert req.params.item_id == "item"
        assert req.params.call_id == "call"
        assert req.params.approval_id == "appr"
        assert req.params.execpolicy_amendment == ["npm"]

    def test_default_message_without_command(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 10,
                "method": "execCommandApproval",
                "params": {},
            }
        )
        assert req.params.message == "Codex wants to run a command"


# ── file change approval params ──────────────────────────────────────


class TestCodexFileChangeApprovalParams:
    """Tests for file-change approval param builder."""

    def test_builds_message_and_extras(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 11,
                "method": "item/fileChange/requestApproval",
                "params": {
                    "reason": "write",
                    "grantRoot": "/workspace",
                    "threadId": "t",
                    "turnId": "u",
                    "itemId": "i",
                },
            }
        )
        assert req.params.message == "Codex wants write access under **/workspace**"
        assert req.params.grant_root == "/workspace"
        assert req.params.reason == "write"
        assert req.params.thread_id == "t"
        assert req.params.turn_id == "u"
        assert req.params.item_id == "i"


# ── permissions approval params ──────────────────────────────────────


class TestCodexPermissionsApprovalParams:
    """Tests for permissions approval param builder."""

    def test_builds_extras(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 12,
                "method": "item/permissions/requestApproval",
                "params": {
                    "cwd": "/app",
                    "reason": "network",
                    "permissions": {"network": True},
                    "threadId": "t",
                    "turnId": "u",
                    "itemId": "i",
                },
            }
        )
        assert req.params.cwd == "/app"
        assert req.params.reason == "network"
        assert req.params.permissions == {"network": True}
        assert req.params.thread_id == "t"
        assert req.params.turn_id == "u"
        assert req.params.item_id == "i"


# ── apply patch approval params ──────────────────────────────────────


class TestCodexApplyPatchApprovalParams:
    """Tests for legacy apply-patch approval param builder."""

    def test_builds_extras_and_sorted_files(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 13,
                "method": "applyPatchApproval",
                "params": {
                    "reason": "patch",
                    "grantRoot": "/repo",
                    "conversationId": "conv",
                    "callId": "call",
                    "fileChanges": {"b.py": {}, "a.py": {}},
                },
            }
        )
        assert req.params.reason == "patch"
        assert req.params.grant_root == "/repo"
        assert req.params.thread_id == "conv"
        assert req.params.call_id == "call"
        assert req.params.files == ["a.py", "b.py"]


# ── _codex_command_preview ───────────────────────────────────────────


class TestCodexCommandPreview:
    """Tests for command preview extraction."""

    def test_string_command(self) -> None:
        assert _codex_command_preview({"command": "npm test"}) == "npm test"

    def test_list_command(self) -> None:
        assert _codex_command_preview({"command": ["npm", "test"]}) == "npm test"

    def test_empty_command(self) -> None:
        assert _codex_command_preview({"command": ""}) is None

    def test_missing_command(self) -> None:
        assert _codex_command_preview({}) is None


# ── _json_preview ────────────────────────────────────────────────────


class TestJsonPreview:
    """Tests for the bounded preview function."""

    def test_simple_object(self) -> None:
        result = _json_preview({"key": "value"})
        assert '"key"' in result

    def test_truncated(self) -> None:
        big = {"k": "x" * 2000}
        result = _json_preview(big)
        assert len(result) <= 1024

    def test_unserializable_falls_back_to_repr(self) -> None:
        class _Unserializable:
            def __repr__(self) -> str:
                return "<unserializable>"

        assert _json_preview(_Unserializable()) == "<unserializable>"