"""Edge tests for Databricks auth helpers in omnigent.chat."""

from __future__ import annotations

import httpx
import pytest

import omnigent.chat as chat_module
from omnigent.inner.databricks_executor import DatabricksAuthError


def _first_auth_header(auth: httpx.Auth, url: str) -> str | None:
    flow = auth.auth_flow(httpx.Request("GET", url))
    request = next(flow)
    flow.close()
    return request.headers.get("Authorization")


def test_stored_databricks_record_token_returns_none_without_workspace_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_workspace_host", lambda _url: None)
    assert chat_module._stored_databricks_record_token("https://app.databricksapps.com") is None


def test_stored_databricks_record_token_returns_sdk_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeAuth:
        def current_token(self) -> str:
            return "record-tok"

    monkeypatch.setattr(
        "omnigent.cli_auth.load_databricks_workspace_host",
        lambda _url: "https://workspace.cloud.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        lambda **kwargs: (_FakeAuth(), kwargs.get("host")),
    )

    token = chat_module._stored_databricks_record_token("https://app.databricksapps.com")
    assert token == "record-tok"


def test_stored_databricks_record_token_swallows_resolution_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.cli_auth.load_databricks_workspace_host",
        lambda _url: "https://workspace.cloud.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        lambda **_kwargs: (_ for _ in ()).throw(DatabricksAuthError("no creds")),
    )
    assert chat_module._stored_databricks_record_token("https://app.databricksapps.com") is None


def test_databricks_token_auth_prefers_static_env_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(chat_module._REMOTE_AUTH_TOKEN_ENV, "  static-tok  ")
    auth = chat_module._DatabricksTokenAuth(server_url="https://ex.databricks.com")
    assert _first_auth_header(auth, "https://ex.databricks.com/v1/x") == "Bearer static-tok"


def test_databricks_token_auth_uses_stored_oidc_before_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(chat_module._REMOTE_AUTH_TOKEN_ENV, raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: "oidc-tok")
    auth = chat_module._DatabricksTokenAuth(server_url="https://ex.databricks.com")
    assert _first_auth_header(auth, "https://ex.databricks.com/v1/x") == "Bearer oidc-tok"


def test_databricks_token_auth_resolves_workspace_host_for_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.inner.databricks_executor as dbx

    class _Cfg:
        def authenticate(self) -> dict[str, str]:
            return {"Authorization": "Bearer host-tok"}

    seen: dict[str, object] = {}

    def _fake_resolve(**kwargs: object) -> tuple[object, str]:
        seen.update(kwargs)
        return dbx._DatabricksBearerAuth(_Cfg(), profile_name=None), "https://workspace"

    monkeypatch.delenv(chat_module._REMOTE_AUTH_TOKEN_ENV, raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr(
        "omnigent.cli_auth.load_databricks_workspace_host",
        lambda _url: "https://workspace.cloud.databricks.com",
    )
    monkeypatch.setattr(dbx, "_resolve_databricks_auth", _fake_resolve)

    auth = chat_module._DatabricksTokenAuth(server_url="https://app.databricksapps.com")
    assert _first_auth_header(auth, "https://app/v1/x") == "Bearer host-tok"
    assert seen.get("host") == "https://workspace.cloud.databricks.com"


def test_databricks_token_auth_leaves_header_unset_when_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(chat_module._REMOTE_AUTH_TOKEN_ENV, raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_workspace_host", lambda _url: None)
    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._resolve_databricks_auth",
        lambda **_kwargs: (_ for _ in ()).throw(DatabricksAuthError("missing")),
    )

    auth = chat_module._DatabricksTokenAuth(server_url="https://ex.databricks.com")
    assert _first_auth_header(auth, "https://ex.databricks.com/v1/x") is None


def _request_after_flow(auth: httpx.Auth, url: str) -> httpx.Request:
    flow = auth.auth_flow(httpx.Request("GET", url))
    request = next(flow)
    flow.close()
    return request


def test_auth_flow_injects_runner_tunnel_token_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner with only its tunnel binding token authenticates via the
    runner-tunnel header (RunnerTokenAuthProvider), even with no bearer."""
    from omnigent.runner.identity import (
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
        RUNNER_TUNNEL_TOKEN_HEADER,
    )

    monkeypatch.delenv(chat_module._REMOTE_AUTH_TOKEN_ENV, raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_workspace_host", lambda _url: None)
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "  runner-bind-tok  ")

    auth = chat_module._DatabricksTokenAuth(server_url="http://omnigent-server")
    request = _request_after_flow(auth, "http://omnigent-server/v1/sessions/conv_x/events")
    assert request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) == "runner-bind-tok"
    # No bearer when no user credential source — the tunnel token stands alone.
    assert request.headers.get("Authorization") is None


def test_auth_flow_sets_both_bearer_and_tunnel_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner.identity import (
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
        RUNNER_TUNNEL_TOKEN_HEADER,
    )

    monkeypatch.setenv(chat_module._REMOTE_AUTH_TOKEN_ENV, "user-bearer")
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "runner-bind-tok")
    auth = chat_module._DatabricksTokenAuth(server_url="http://omnigent-server")
    request = _request_after_flow(auth, "http://omnigent-server/v1/x")
    assert request.headers.get("Authorization") == "Bearer user-bearer"
    assert request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) == "runner-bind-tok"


def test_server_auth_returns_interceptor_for_runner_tunnel_token_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_server_auth`` must return the interceptor when only the runner
    tunnel token is present — else the forwarder runs with ``auth=None``
    and every runner->server call 401s."""
    from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR

    monkeypatch.delenv(chat_module._REMOTE_AUTH_TOKEN_ENV, raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_workspace_host", lambda _url: None)
    monkeypatch.setattr(chat_module, "_read_databrickscfg", lambda _profile: None)
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "runner-bind-tok")

    auth = chat_module._server_auth(server_url="http://omnigent-server")
    assert auth is not None
