"""Tests for accounts auth route helper functions.

The full accounts auth flow requires an account store with passwords,
so we test the pure-function helpers directly.
"""

from __future__ import annotations

import pytest
from starlette.responses import Response

from omnigent.server.routes.accounts_auth import (
    _clear_session_cookie,
    _cookie_samesite,
    _redact_for_log,
    _set_session_cookie,
    _validate_username,
)


class TestValidateUsername:
    """Tests for username format and reserved-name checks."""

    def test_valid_lowercase(self) -> None:
        assert _validate_username("alice") is None

    def test_valid_with_dots(self) -> None:
        assert _validate_username("alice.bob") is None

    def test_valid_with_hyphens(self) -> None:
        assert _validate_username("alice-bob") is None

    def test_valid_email(self) -> None:
        assert _validate_username("alice@example.com") is None

    def test_reserved_local(self) -> None:
        result = _validate_username("local")
        assert result is not None
        assert "reserved" in result

    def test_reserved_public(self) -> None:
        result = _validate_username("__public__")
        assert result is not None
        assert "reserved" in result

    def test_uppercase_lowercased_then_valid(self) -> None:
        # _validate_username lowercases before checking, so ALICE -> alice is valid
        result = _validate_username("ALICE")
        assert result is None

    def test_empty_string(self) -> None:
        result = _validate_username("")
        assert result is not None

    def test_special_chars_rejected(self) -> None:
        result = _validate_username("alice<script>")
        assert result is not None


class TestRedactForLog:
    """Tests for log redaction of user IDs."""

    def test_short_id(self) -> None:
        assert _redact_for_log("ab") == "a***"

    def test_normal_id(self) -> None:
        result = _redact_for_log("alice@example.com")
        assert result.startswith("ali")
        assert "***" in result
        assert "len=" in result

    def test_min_length(self) -> None:
        assert _redact_for_log("a") == "a***"


class TestCookieSameSite:
    """`_cookie_samesite()` is the env-gated SameSite policy (BDP-2501)."""

    def test_defaults_to_lax(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMNIGENT_COOKIE_SAMESITE", raising=False)
        assert _cookie_samesite() == "lax"

    def test_none_opts_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNIGENT_COOKIE_SAMESITE", "none")
        assert _cookie_samesite() == "none"

    def test_none_is_trimmed_and_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIGENT_COOKIE_SAMESITE", "  None ")
        assert _cookie_samesite() == "none"

    def test_unrecognized_value_stays_lax(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only the literal "none" opts in; anything else (incl. "strict") is lax.
        monkeypatch.setenv("OMNIGENT_COOKIE_SAMESITE", "strict")
        assert _cookie_samesite() == "lax"


def _cookie_header(response: Response) -> str:
    return response.headers.get("set-cookie", "").lower()


class TestSessionCookieSameSite:
    """The session cookie honors the SameSite policy and pairs None with Secure.

    SameSite=None is invalid without Secure (browsers drop it), so the setter
    forces Secure for the embed mode even when the base deploy passes
    ``secure=False`` (BDP-2501).
    """

    def test_setter_defaults_to_lax(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMNIGENT_COOKIE_SAMESITE", raising=False)
        resp = Response()
        _set_session_cookie(
            resp, "tok", cookie_name="__Host-ap_session", secure=True, max_age_seconds=3600
        )
        assert "samesite=lax" in _cookie_header(resp)

    def test_setter_none_forces_secure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNIGENT_COOKIE_SAMESITE", "none")
        resp = Response()
        # secure=False from the caller — None must still force Secure.
        _set_session_cookie(
            resp, "tok", cookie_name="__Host-ap_session", secure=False, max_age_seconds=3600
        )
        header = _cookie_header(resp)
        assert "samesite=none" in header
        assert "secure" in header

    def test_clear_matches_setter_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The clearer must mirror the setter's attributes or the browser won't
        # match-and-delete the cookie (logout would silently leave it).
        monkeypatch.setenv("OMNIGENT_COOKIE_SAMESITE", "none")
        resp = Response()
        _clear_session_cookie(resp, cookie_name="__Host-ap_session", secure=False)
        header = _cookie_header(resp)
        assert "samesite=none" in header
        assert "secure" in header
