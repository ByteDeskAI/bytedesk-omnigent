"""Tests for the native ``bytedesk_generate_image`` agent tool."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from bytedesk_omnigent.tools.image_generation_tools import (
    BytedeskGenerateImageTool,
    _OpenAIImageGenerationClient,
)
from omnigent.tools.base import ToolContext

_CTX = ToolContext(task_id="task_1", agent_id="ag_1", conversation_id="conv_1")
_PNG_BYTES = b"\x89PNG\r\n\x1a\nimage-bytes"


def _make_tool(handler) -> tuple[BytedeskGenerateImageTool, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def _capturing(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    http = httpx.Client(
        base_url="https://api.openai.test/v1",
        transport=httpx.MockTransport(_capturing),
    )
    client = _OpenAIImageGenerationClient(
        base_url="https://api.openai.test/v1",
        api_key="sk-test",
        client=http,
    )
    return BytedeskGenerateImageTool(client=client), captured


def _call(tool: BytedeskGenerateImageTool, **args: Any) -> dict[str, Any]:
    return json.loads(tool.invoke(json.dumps(args), _CTX))


def _patch_stores(monkeypatch) -> tuple[list[dict[str, Any]], list[tuple[str, bytes]]]:
    stored_files: list[dict[str, Any]] = []
    stored_artifacts: list[tuple[str, bytes]] = []

    class _FakeFileRecord:
        def __init__(self, file_id: str) -> None:
            self.id = file_id

    class _FakeFileStore:
        def create(
            self,
            filename: str,
            bytes: int,
            content_type: str,
            session_id: str | None = None,
        ) -> _FakeFileRecord:
            stored_files.append(
                {
                    "filename": filename,
                    "bytes": bytes,
                    "content_type": content_type,
                    "session_id": session_id,
                }
            )
            return _FakeFileRecord("file_img_123")

    class _FakeArtifactStore:
        def put(self, key: str, data: bytes) -> None:
            stored_artifacts.append((key, data))

    monkeypatch.setattr("omnigent.runtime.get_file_store", lambda: _FakeFileStore())
    monkeypatch.setattr("omnigent.runtime.get_artifact_store", lambda: _FakeArtifactStore())
    return stored_files, stored_artifacts


def test_generate_image_posts_to_openai_and_stores_session_file(monkeypatch) -> None:
    stored_files, stored_artifacts = _patch_stores(monkeypatch)
    encoded = base64.b64encode(_PNG_BYTES).decode()
    tool, captured = _make_tool(
        lambda _r: httpx.Response(
            200,
            json={
                "data": [{"b64_json": encoded, "revised_prompt": "final prompt"}],
                "usage": {"input_tokens": 12},
            },
        )
    )

    result = _call(
        tool,
        prompt="Generate a homepage hero image",
        filename="hero asset",
        size="3840x2160",
        quality="LOW",
        output_format="PNG",
    )

    assert result["ok"] is True
    assert result["file_id"] == "file_img_123"
    assert result["filename"] == "hero-asset.png"
    assert result["content_type"] == "image/png"
    assert result["bytes"] == len(_PNG_BYTES)
    assert result["model"] == "gpt-image-2"
    assert result["revised_prompt"] == "final prompt"
    assert result["usage"] == {"input_tokens": 12}
    assert stored_files == [
        {
            "filename": "hero-asset.png",
            "bytes": len(_PNG_BYTES),
            "content_type": "image/png",
            "session_id": "conv_1",
        }
    ]
    assert stored_artifacts == [("file_img_123", _PNG_BYTES)]

    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/images/generations"
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert json.loads(request.content) == {
        "model": "gpt-image-2",
        "prompt": "Generate a homepage hero image",
        "size": "3840x2160",
        "quality": "low",
        "output_format": "png",
        "background": "opaque",
    }


def test_missing_prompt_returns_structured_error_without_network() -> None:
    tool, captured = _make_tool(lambda _r: httpx.Response(500))

    result = _call(tool)

    assert result == {"ok": False, "error": "missing required 'prompt'"}
    assert captured == []


def test_invalid_options_return_structured_error_without_network() -> None:
    tool, captured = _make_tool(lambda _r: httpx.Response(500))

    result = _call(tool, prompt="x", size="9999x9999")

    assert result == {"ok": False, "error": "invalid_image_generation_options"}
    assert captured == []


def test_requires_session_id_before_network() -> None:
    tool, captured = _make_tool(lambda _r: httpx.Response(500))
    ctx = ToolContext(task_id="task_1", agent_id="ag_1")

    result = json.loads(tool.invoke(json.dumps({"prompt": "x"}), ctx))

    assert result == {"ok": False, "error": "image_generation_requires_session"}
    assert captured == []


def test_invalid_arguments_json_is_graceful() -> None:
    tool, _ = _make_tool(lambda _r: httpx.Response(500))

    result = json.loads(tool.invoke("{not json", _CTX))

    assert result == {"ok": False, "error": "invalid_arguments_json"}


def test_missing_openai_key_returns_configured_error(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BYTEDESK_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda _name: None)
    tool = BytedeskGenerateImageTool(
        client=_OpenAIImageGenerationClient(base_url="https://api.openai.test/v1")
    )

    result = _call(tool, prompt="x")

    assert result == {"ok": False, "error": "openai_image_generation_not_configured"}


def test_http_error_returns_structured_status(monkeypatch) -> None:
    _patch_stores(monkeypatch)
    tool, _ = _make_tool(lambda _r: httpx.Response(403, json={"error": {"message": "nope"}}))

    result = _call(tool, prompt="x")

    assert result == {
        "ok": False,
        "error": "openai_image_generation_http_error",
        "status": 403,
    }


def test_transport_error_returns_structured_error(monkeypatch) -> None:
    _patch_stores(monkeypatch)

    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    tool, _ = _make_tool(_boom)

    result = _call(tool, prompt="x")

    assert result == {"ok": False, "error": "openai_image_generation_request_failed"}


def test_bad_openai_response_returns_structured_error(monkeypatch) -> None:
    _patch_stores(monkeypatch)
    tool, _ = _make_tool(lambda _r: httpx.Response(200, json={"data": [{}]}))

    result = _call(tool, prompt="x")

    assert result == {"ok": False, "error": "openai_image_generation_bad_response"}


def test_tool_name_schema_and_extension_registration() -> None:
    assert BytedeskGenerateImageTool.name() == "bytedesk_generate_image"
    schema = BytedeskGenerateImageTool().get_schema()
    assert schema["function"]["name"] == "bytedesk_generate_image"
    assert schema["function"]["parameters"]["required"] == ["prompt"]
    backgrounds = schema["function"]["parameters"]["properties"]["background"]["enum"]
    assert "transparent" not in backgrounds

    from bytedesk_omnigent.extension import BytedeskExtension

    factories = BytedeskExtension().tool_factories()
    assert "bytedesk_generate_image" in factories
    tool = factories["bytedesk_generate_image"](object())
    assert tool.name() == "bytedesk_generate_image"
