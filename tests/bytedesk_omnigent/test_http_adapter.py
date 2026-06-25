"""Tests for the shared SaaS-tool HTTP adapter base (BDP-2483).

``first_secret`` + the ``HttpToolClient`` skeleton were extracted from the four
per-provider tool clients (github/jira/confluence/slack), which had copy-pasted
them verbatim. The provider test suites exercise this through real operations;
these tests pin the shared base directly.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from bytedesk_omnigent.tools._http_adapter import HttpToolClient, first_secret


def test_first_secret_returns_first_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {"A": "  ", "B": "tok", "C": "other"}
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret", lambda name: values.get(name)
    )
    assert first_secret(("A", "B", "C")) == "tok"


def test_first_secret_empty_when_all_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda name: None)
    assert first_secret(("A", "B")) == ""


class _StubClient(HttpToolClient):
    """Minimal concrete subclass exercising the shared skeleton."""

    def __init__(self, client: httpx.Client) -> None:
        self._base_url = "https://example.test"
        self._client = client
        self.configured = False

    def _require_configured(self) -> None:
        self.configured = True

    def _headers(self) -> dict[str, str]:
        return {"X-Test": "1"}


def test_http_is_lazy_and_cached() -> None:
    client = httpx.Client(base_url="https://example.test")
    stub = _StubClient(client)
    assert stub._http() is client
    assert stub._http() is stub._http()


def test_request_requires_configured_and_sends_headers() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers.get("X-Test")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    stub = _StubClient(httpx.Client(base_url="https://example.test", transport=transport))
    resp = stub._request("GET", "/ping")

    assert stub.configured is True
    assert captured["header"] == "1"
    assert resp.json() == {"ok": True}


def test_base_stubs_must_be_overridden() -> None:
    bare = HttpToolClient()
    with pytest.raises(NotImplementedError):
        bare._require_configured()
    with pytest.raises(NotImplementedError):
        bare._headers()
