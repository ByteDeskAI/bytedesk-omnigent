"""Edge tests for session/agent listing and bundle helpers in ``tool_dispatch.py``."""

from __future__ import annotations

import json
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from omnigent.runner import app as runner_app
from omnigent.runner.tool_dispatch import (
    _agent_bundle_filename,
    _build_session_create_body,
    _bundle_local_agent_source,
    _child_rows_to_entries,
    _finalize_created_session,
    _project_agent_list,
    _scan_local_agent_configs,
)
from omnigent.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE


def test_build_session_create_body_forces_parent_and_optional_fields() -> None:
    body = _build_session_create_body(
        "ag_child",
        "conv_parent",
        title="researcher:auth",
        message="start here",
    )
    assert body == {
        "agent_id": "ag_child",
        "parent_session_id": "conv_parent",
        "title": "researcher:auth",
        "initial_items": [
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "start here"}],
                },
            }
        ],
    }


def test_build_session_create_body_omits_empty_title_and_message() -> None:
    body = _build_session_create_body("ag_child", "conv_parent", title="", message=None)
    assert body == {"agent_id": "ag_child", "parent_session_id": "conv_parent"}


def test_finalize_created_session_registers_child_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: list[tuple[str, dict[str, Any]]] = []
    published: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(
        runner_app,
        "register_child_session",
        lambda child_id, **kwargs: registered.append((child_id, kwargs)),
    )

    def _capture_publish(session_id: str, event: dict[str, Any]) -> None:
        published.append((session_id, event))

    result = json.loads(
        _finalize_created_session(
            {"id": "conv_child", "agent_name": "researcher", "status": "created"},
            conversation_id="conv_parent",
            agent_id="ag_child",
            title="auth task",
            publish_event=_capture_publish,
        )
    )

    assert result == {
        "conversation_id": "conv_child",
        "kind": "sub_agent",
        "agent_id": "ag_child",
        "agent_name": "researcher",
        "title": "auth task",
        "status": "created",
    }
    assert registered == [
        (
            "conv_child",
            {
                "parent_session_id": "conv_parent",
                "title": "auth task",
                "tool": "researcher",
                "session_name": "auth task",
            },
        )
    ]
    assert len(published) == 1
    parent_id, event = published[0]
    assert parent_id == "conv_parent"
    assert event["type"] == "session.created"
    assert event["child_session_id"] == "conv_child"


def test_finalize_created_session_skips_publish_without_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_app, "register_child_session", lambda *_a, **_k: None)
    result = json.loads(
        _finalize_created_session(
            {"id": "conv_child", "status": "idle"},
            conversation_id="conv_parent",
            agent_id="ag_child",
            title=42,
            publish_event=None,
        )
    )
    assert result["title"] is None
    assert result["agent_name"] is None
    assert result["status"] == "idle"


@pytest.mark.parametrize(
    ("dest", "expected"),
    [
        ("bundle.tar.gz", "bundle.tar.gz"),
        (None, "researcher-v3.tar.gz"),
        ("bad/path.tgz", None),
        (".", None),
        ("..", None),
    ],
)
def test_agent_bundle_filename_rejects_paths_and_defaults(
    dest: object,
    expected: str | None,
) -> None:
    assert _agent_bundle_filename(dest, "researcher", "3") == expected


def test_agent_bundle_filename_treats_non_string_dest_as_absent() -> None:
    assert _agent_bundle_filename(123, "helper", "1") == "helper-v1.tar.gz"


def test_agent_bundle_filename_sanitizes_agent_name() -> None:
    assert _agent_bundle_filename(None, "my agent!", "2") == "my_agent_-v2.tar.gz"
    assert _agent_bundle_filename(None, "", "0") == "agent-v0.tar.gz"


def test_scan_local_agent_configs_reads_valid_yaml(tmp_path: Path) -> None:
    configs = tmp_path / "agent-configs"
    configs.mkdir()
    (configs / "helper.yaml").write_text("name: helper\ndescription: A helper agent\n")
    (configs / "broken.yaml").write_text(":\n  bad: [")
    (configs / "list.yaml").write_text("- not-a-map\n")

    entries = _scan_local_agent_configs(configs)
    assert entries == [
        {
            "name": "helper",
            "path": str(configs / "helper.yaml"),
            "description": "A helper agent",
        }
    ]


def test_scan_local_agent_configs_returns_empty_for_missing_dir(tmp_path: Path) -> None:
    assert _scan_local_agent_configs(tmp_path / "missing") == []


def test_project_agent_list_maps_builtin_and_session_rows() -> None:
    projected = _project_agent_list(
        [{"id": "ag_1", "name": "polly", "description": "orchestrator", "harness": "pi"}],
        [{"id": "conv_1", "agent_id": "ag_1", "agent_name": "polly", "status": "idle"}],
        [{"name": "local", "path": "/tmp/helper.yaml", "description": None}],
    )
    assert projected == {
        "builtins": [
            {
                "agent_id": "ag_1",
                "name": "polly",
                "description": "orchestrator",
                "harness": "pi",
            }
        ],
        "session_agents": [
            {
                "session_id": "conv_1",
                "agent_id": "ag_1",
                "agent_name": "polly",
                "status": "idle",
            }
        ],
        "local_configs": [{"name": "local", "path": "/tmp/helper.yaml", "description": None}],
    }


def test_child_rows_to_entries_skips_closed_and_malformed_rows() -> None:
    rows = [
        {
            "id": "conv_open",
            "title": "researcher:auth",
            "tool": "researcher",
            "session_name": "auth",
            "labels": {},
        },
        {
            "id": "conv_closed",
            "title": "researcher:done",
            "tool": "researcher",
            "session_name": "done",
            "labels": {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
        },
        {"id": "conv_plain", "title": "standalone", "tool": "researcher", "session_name": "x"},
        {"id": "conv_empty", "title": None, "tool": "researcher", "session_name": "x"},
    ]
    assert _child_rows_to_entries(rows) == [
        {
            "agent": "researcher",
            "title": "auth",
            "conversation_id": "conv_open",
        }
    ]


def test_bundle_local_agent_source_materializes_yaml_agent(
    tmp_path: Path,
) -> None:
    agent_yaml = tmp_path / "helper.yaml"
    agent_yaml.write_text(
        "spec_version: 1\n"
        "name: helper\n"
        "description: test helper\n"
        "executor:\n"
        "  harness: claude-sdk\n"
    )
    bundle_bytes = _bundle_local_agent_source(agent_yaml)
    with tarfile.open(fileobj=BytesIO(bundle_bytes), mode="r:gz") as archive:
        names = archive.getnames()
    assert names
    assert any(name.endswith(("helper.yaml", "config.yaml")) for name in names)


def test_bundle_local_agent_source_passes_through_prebuilt_tar_gz(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "ready.tar.gz"
    bundle_path.write_bytes(b"prebuilt-bundle")
    assert _bundle_local_agent_source(bundle_path) == b"prebuilt-bundle"


def test_bundle_local_agent_source_raises_for_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _bundle_local_agent_source(tmp_path / "missing.yaml")


@pytest.mark.asyncio
async def test_post_child_first_message_returns_none_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner.tool_dispatch import _post_child_first_message

    class _FakeResponse:
        status_code = 200

    class _FakeClient:
        async def post(self, *_args: object, **_kwargs: object) -> _FakeResponse:
            return _FakeResponse()

    assert await _post_child_first_message("conv_child", "hello", _FakeClient()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_post_child_first_message_returns_error_json_on_http_failure() -> None:
    from omnigent.runner.tool_dispatch import _post_child_first_message

    class _FakeResponse:
        status_code = 500
        text = "server error"

    class _FakeClient:
        async def post(self, *_args: object, **_kwargs: object) -> _FakeResponse:
            return _FakeResponse()

    raw = await _post_child_first_message("conv_child", "hello", _FakeClient())  # type: ignore[arg-type]
    assert raw is not None
    payload = json.loads(raw)
    assert payload["conversation_id"] == "conv_child"
    assert "message failed" in payload["error"]


@pytest.mark.asyncio
async def test_post_child_first_message_returns_error_json_on_transport_error() -> None:
    from omnigent.runner.tool_dispatch import _post_child_first_message

    class _FakeClient:
        async def post(self, *_args: object, **_kwargs: object) -> None:
            raise httpx.ConnectError("offline", request=MagicMock())

    raw = await _post_child_first_message("conv_child", "hello", _FakeClient())  # type: ignore[arg-type]
    assert raw is not None
    payload = json.loads(raw)
    assert payload["conversation_id"] == "conv_child"
    assert "offline" in payload["error"]
