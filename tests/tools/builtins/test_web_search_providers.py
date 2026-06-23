"""Direct unit tests for web search provider implementations."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from omnigent.tools.builtins.web_search_google import (
    _format_results,
    _search_google,
)
from omnigent.tools.builtins.web_search_perplexity import (
    _format_response,
    _perplexity_url,
    _search_perplexity,
)


def test_google_search_missing_credentials_returns_config_error() -> None:
    """Google search requires api_key and engine_id in spec config."""
    result = _search_google("python", {})
    assert "api_key" in result
    assert "engine_id" in result


def test_google_search_http_status_error() -> None:
    """HTTP failures surface as readable Google search errors."""
    response = MagicMock()
    response.status_code = 403
    error = httpx.HTTPStatusError(
        "forbidden",
        request=MagicMock(),
        response=response,
    )
    with patch("omnigent.tools.builtins.web_search_google.httpx.get", side_effect=error):
        result = _search_google("python", {"api_key": "k", "engine_id": "cx"})
    assert result == "Google search error: HTTP 403"


def test_google_search_connect_error() -> None:
    """Network failures surface as readable Google search errors."""
    with patch(
        "omnigent.tools.builtins.web_search_google.httpx.get",
        side_effect=httpx.ConnectError("offline"),
    ):
        result = _search_google("python", {"api_key": "k", "engine_id": "cx"})
    assert "Google search error:" in result
    assert "offline" in result


def test_google_format_results_empty_items() -> None:
    """An empty CSE payload returns a friendly no-results message."""
    assert _format_results({}) == "No results found."


def test_google_search_success_formats_numbered_results() -> None:
    """A successful CSE response is formatted for the LLM."""
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "items": [
            {
                "title": "Python Docs",
                "link": "https://docs.python.org",
                "snippet": "Welcome to Python.",
            },
        ],
    }
    with patch(
        "omnigent.tools.builtins.web_search_google.httpx.get",
        return_value=fake_response,
    ):
        result = _search_google("python", {"api_key": "k", "engine_id": "cx"})
    assert "1. Python Docs" in result
    assert "https://docs.python.org" in result
    assert "Welcome to Python." in result


def test_perplexity_search_missing_api_key_returns_config_error() -> None:
    """Perplexity search requires api_key in spec config."""
    result = _search_perplexity("python", {})
    assert "api_key" in result


def test_perplexity_search_http_status_error() -> None:
    """HTTP failures surface as readable Perplexity search errors."""
    response = MagicMock()
    response.status_code = 401
    error = httpx.HTTPStatusError(
        "unauthorized",
        request=MagicMock(),
        response=response,
    )
    with patch("omnigent.tools.builtins.web_search_perplexity.httpx.post", side_effect=error):
        result = _search_perplexity("python", {"api_key": "k"})
    assert result == "Perplexity search error: HTTP 401"


def test_perplexity_search_timeout_error() -> None:
    """Timeouts surface as readable Perplexity search errors."""
    with patch(
        "omnigent.tools.builtins.web_search_perplexity.httpx.post",
        side_effect=httpx.TimeoutException("slow"),
    ):
        result = _search_perplexity("python", {"api_key": "k"})
    assert "Perplexity search error:" in result


def test_perplexity_format_response_empty_choices() -> None:
    """An empty completion payload returns a friendly no-answer message."""
    assert _format_response({"choices": []}) == "No answer returned."


def test_perplexity_search_success_appends_citations() -> None:
    """A successful Perplexity response includes answer text and sources."""
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "Python is a language."}}],
        "citations": ["https://python.org", "https://docs.python.org"],
    }
    with patch(
        "omnigent.tools.builtins.web_search_perplexity.httpx.post",
        return_value=fake_response,
    ):
        result = _search_perplexity("python", {"api_key": "k"})
    assert "Python is a language." in result
    assert "[1] https://python.org" in result
    assert "[2] https://docs.python.org" in result


def test_perplexity_url_honors_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests can redirect Perplexity traffic via OMNIGENT_PERPLEXITY_BASE_URL."""
    monkeypatch.setenv("OMNIGENT_PERPLEXITY_BASE_URL", "http://127.0.0.1:9/perplexity")
    assert _perplexity_url() == "http://127.0.0.1:9/perplexity"