"""E2E: generated image tool output renders inline in chat.

The ByteDesk image-generation tool returns a JSON payload containing a
session file id. The chat UI must treat that payload as a generated artifact,
fetch the session resource bytes, and render the image inline instead of only
showing the raw tool output or a download chip.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUILD_OUTPUT = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
_SESSION_ID = "conv_imagegen_inline"
_FILE_ID = "file_launch_hero"
_RESPONSE_ID = "resp_imagegen_inline"
_CALL_ID = "call_imagegen_inline"
_FILENAME = "launch-hero.png"


class _SpaHandler(SimpleHTTPRequestHandler):
    """Serve the built SPA with BrowserRouter fallback to index.html."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(_BUILD_OUTPUT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        requested = (_BUILD_OUTPUT / parsed.path.lstrip("/")).resolve()
        if parsed.path != "/" and not requested.exists():
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _items() -> list[dict[str, object]]:
    """Persisted conversation items for a completed generated-image tool call."""
    return [
        {
            "id": "fc_imagegen_inline",
            "response_id": _RESPONSE_ID,
            "type": "function_call",
            "status": "completed",
            "model": "brand-and-creative-director",
            "name": "bytedesk_generate_image",
            "arguments": json.dumps({"prompt": "Generate a launch hero image."}),
            "call_id": _CALL_ID,
        },
        {
            "id": "fco_imagegen_inline",
            "response_id": _RESPONSE_ID,
            "type": "function_call_output",
            "status": "completed",
            "call_id": _CALL_ID,
            "output": json.dumps(
                {
                    "ok": True,
                    "file_id": _FILE_ID,
                    "filename": _FILENAME,
                    "content_type": "image/png",
                }
            ),
        },
    ]


def _session_snapshot() -> dict[str, object]:
    return {
        "id": _SESSION_ID,
        "object": "conversation",
        "title": "Image generation proof",
        "agent_id": "ag_brand",
        "agent_name": "brand-and-creative-director",
        "runner_id": None,
        "status": "idle",
        "created_at": 0,
        "updated_at": 0,
        "labels": {},
        "permission_level": None,
        "runner_online": True,
        "host_online": None,
        "items": _items(),
        "pending_elicitations": [],
        "pending_inputs": [],
    }


def _session_list() -> dict[str, object]:
    row = {k: v for k, v in _session_snapshot().items() if k != "items"}
    return {
        "object": "list",
        "data": [row],
        "first_id": _SESSION_ID,
        "last_id": _SESSION_ID,
        "has_more": False,
    }


def _items_page() -> dict[str, object]:
    data = _items()
    return {
        "object": "list",
        "data": data,
        "first_id": data[0]["id"],
        "last_id": data[-1]["id"],
        "has_more": False,
    }


def _register_api_routes(page: Page) -> None:
    """Stub the server API calls the chat route needs for hydration."""

    def fulfill_json(route, body: dict[str, object]) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def handle(route) -> None:
        request = route.request
        path = urlparse(request.url).path
        method = request.method

        if path == "/v1/me":
            fulfill_json(route, {"user_id": "local"})
            return
        if path == "/health":
            fulfill_json(
                route,
                {
                    "status": "ok",
                    "sessions": {
                        _SESSION_ID: {
                            "runner_online": True,
                            "host_online": None,
                        }
                    },
                },
            )
            return
        if path == "/v1/agents":
            fulfill_json(
                route,
                {
                    "data": [
                        {
                            "id": "ag_brand",
                            "name": "brand-and-creative-director",
                            "display_name": "Brand and Creative Director",
                            "harness": "claude-sdk",
                            "skills": ["imagegen"],
                        }
                    ]
                },
            )
            return
        if path == "/v1/hosts":
            fulfill_json(route, {"data": []})
            return
        if path == f"/v1/sessions/{_SESSION_ID}/resources/files/{_FILE_ID}/content":
            route.fulfill(
                status=200,
                content_type="image/png",
                body=_TINY_PNG,
                headers={
                    "Content-Disposition": f'attachment; filename="{_FILENAME}"',
                    "X-Content-Type-Options": "nosniff",
                },
            )
            return
        if path == f"/v1/sessions/{_SESSION_ID}/stream":
            route.fulfill(status=200, content_type="text/event-stream", body="data: [DONE]\n\n")
            return
        if path == f"/v1/sessions/{_SESSION_ID}/items":
            fulfill_json(route, _items_page())
            return
        if path == f"/v1/sessions/{_SESSION_ID}/agent":
            fulfill_json(
                route,
                {
                    "id": "ag_brand",
                    "name": "brand-and-creative-director",
                    "display_name": "Brand and Creative Director",
                    "harness": "claude-sdk",
                    "skills": ["imagegen"],
                },
            )
            return
        if path == "/v1/agents/ag_brand/blueprint":
            fulfill_json(route, {"nodes": [], "edges": []})
            return
        if path == f"/v1/sessions/{_SESSION_ID}" and method == "GET":
            fulfill_json(route, _session_snapshot())
            return
        if path == "/v1/sessions" and method == "GET":
            fulfill_json(route, _session_list())
            return
        if path.startswith("/v1/"):
            fulfill_json(route, {})
            return
        route.continue_()

    page.route("**/*", handle)


@contextmanager
def _serve_static_spa() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SpaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_generated_image_tool_output_renders_inline(
    page: Page,
    built_spa: None,
) -> None:
    """A hydrated image-generation tool result appears as a decoded inline image."""
    del built_spa

    _register_api_routes(page)
    with _serve_static_spa() as base_url:
        page.goto(f"{base_url}/c/{_SESSION_ID}")

        artifact = page.get_by_test_id("assistant-file-artifact")
        expect(artifact).to_be_visible(timeout=30_000)

        image = page.get_by_role("img", name=_FILENAME)
        expect(image).to_be_visible(timeout=30_000)
        expect(image).to_have_attribute(
            "src",
            re.compile(
                rf"/v1/sessions/{re.escape(_SESSION_ID)}/resources/files/{re.escape(_FILE_ID)}/content"
            ),
        )

        image_handle = image.element_handle(timeout=30_000)
        assert image_handle is not None
        page.wait_for_function(
            "(el) => el instanceof HTMLImageElement && el.complete && el.naturalWidth > 0",
            arg=image_handle,
            timeout=30_000,
        )
