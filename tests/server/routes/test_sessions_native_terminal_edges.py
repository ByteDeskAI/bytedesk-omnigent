"""Edge tests for native terminal ensure error helpers in ``sessions.py``."""

from __future__ import annotations

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import (
    _NATIVE_TERMINAL_ENSURE_FAILED_CODE,
    _native_terminal_ensure_transport_error,
    _native_terminal_failure_from_runner_response,
    _native_terminal_name_for_harness,
)


def test_native_terminal_failure_from_runner_preserves_structured_error() -> None:
    request = httpx.Request("POST", "http://runner/v1/sessions/conv/res/terminals")
    response = httpx.Response(
        500,
        request=request,
        json={
            "error": {
                "code": "cli_missing",
                "message": "claude binary not found in PATH",
            }
        },
    )

    error = _native_terminal_failure_from_runner_response(response, display_name="Claude")

    assert error.code == "cli_missing"
    assert error.message == "claude binary not found in PATH"
    assert error.source == "execution"


def test_native_terminal_failure_from_runner_falls_back_on_opaque_body() -> None:
    request = httpx.Request("POST", "http://runner/v1/sessions/conv/res/terminals")
    response = httpx.Response(500, request=request, text="Internal Server Error")

    error = _native_terminal_failure_from_runner_response(response, display_name="Codex")

    assert error.code == _NATIVE_TERMINAL_ENSURE_FAILED_CODE
    assert "malformed runner response" in error.message
    assert "HTTP 500" in error.message


def test_native_terminal_ensure_transport_error_includes_detail() -> None:
    error = _native_terminal_ensure_transport_error(
        httpx.ConnectError("connection refused"),
        display_name="Claude",
    )

    assert error.code == _NATIVE_TERMINAL_ENSURE_FAILED_CODE
    assert "connection refused" in error.message


def test_native_terminal_ensure_transport_error_without_detail() -> None:
    error = _native_terminal_ensure_transport_error(
        ConnectionError(""),
        display_name="Codex",
    )

    assert error.code == _NATIVE_TERMINAL_ENSURE_FAILED_CODE
    assert error.message == "Native Codex terminal ensure request failed."


def test_native_terminal_name_for_harness_rejects_unknown_harness() -> None:
    with pytest.raises(OmnigentError) as exc:
        _native_terminal_name_for_harness("not-a-native-harness")

    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "Unsupported native terminal session" in str(exc.value)
