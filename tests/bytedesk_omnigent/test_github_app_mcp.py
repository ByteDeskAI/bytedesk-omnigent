from __future__ import annotations

import time
from typing import Any

import httpx

from bytedesk_omnigent import github_app_mcp
from bytedesk_omnigent.github_app_mcp import GitHubAppClient, _repo


class _FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def post(self, path: str, headers: dict[str, str]) -> httpx.Response:
        self.calls.append(("POST", path, {"headers": headers}))
        return httpx.Response(
            201,
            json={"token": "installation-token", "expires_at": "2099-01-01T00:00:00Z"},
            request=httpx.Request("POST", f"https://api.github.com{path}"),
        )

    def request(
        self, method: str, path: str, headers: dict[str, str], **kwargs: Any
    ) -> httpx.Response:
        self.calls.append((method, path, {"headers": headers, **kwargs}))
        return httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request(method, f"https://api.github.com{path}"),
        )


class _FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        if path.endswith("/git/ref/heads/develop"):
            return {"object": {"sha": "a" * 40}}
        return {"ok": True, **kwargs}


def _configure_env(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "Iv-test")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "test-private-key")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "123")
    monkeypatch.setattr(github_app_mcp.jwt, "encode", lambda *_args, **_kwargs: "signed.jwt")


def test_repo_accepts_owner_name_and_github_url() -> None:
    assert _repo("ByteDeskAI/bytedesk-platform") == "ByteDeskAI/bytedesk-platform"
    assert (
        _repo("https://github.com/ByteDeskAI/bytedesk-platform.git")
        == "ByteDeskAI/bytedesk-platform"
    )


def test_installation_token_is_minted_and_cached(monkeypatch) -> None:
    _configure_env(monkeypatch)
    fake = _FakeHttp()
    client = GitHubAppClient(http=fake)  # type: ignore[arg-type]

    first = client.request("GET", "/repos/ByteDeskAI/bytedesk-platform")
    second = client.request("GET", "/repos/ByteDeskAI/bytedesk-openclaw")

    assert first == {"ok": True}
    assert second == {"ok": True}
    mint_calls = [call for call in fake.calls if call[1].endswith("/access_tokens")]
    assert len(mint_calls) == 1
    api_calls = [call for call in fake.calls if call[0] == "GET"]
    assert api_calls[0][2]["headers"]["Authorization"] == "Bearer installation-token"


def test_cached_token_refreshes_near_expiry(monkeypatch) -> None:
    _configure_env(monkeypatch)
    fake = _FakeHttp()
    client = GitHubAppClient(http=fake)  # type: ignore[arg-type]

    client.request("GET", "/repos/ByteDeskAI/bytedesk-platform")
    assert client._token is not None
    client._token.expires_at = time.time() + 1
    client.request("GET", "/repos/ByteDeskAI/bytedesk-openclaw")

    mint_calls = [call for call in fake.calls if call[1].endswith("/access_tokens")]
    assert len(mint_calls) == 2


def test_create_branch_resolves_full_ref_before_posting(monkeypatch) -> None:
    fake = _FakeGitHub()
    monkeypatch.setattr(github_app_mcp, "_github_instance", fake)

    result = github_app_mcp.create_branch(
        "ByteDeskAI/bytedesk-platform",
        "feature/test",
        from_ref="refs/heads/develop",
    )

    assert result["json"] == {"ref": "refs/heads/feature/test", "sha": "a" * 40}
    assert fake.calls[0] == (
        "GET",
        "/repos/ByteDeskAI/bytedesk-platform/git/ref/heads/develop",
        {},
    )
