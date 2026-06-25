"""GitHub App-backed MCP server for ByteDesk engineering agents.

Runs as a small Streamable HTTP MCP service in the Omnigent cluster. It keeps
the GitHub App private key in the service environment, mints short-lived
installation tokens on demand, and exposes a focused set of repository tools to
agents through MCP. Agent images only receive ``GITHUB_MCP_URL``.
"""

from __future__ import annotations

import base64
import calendar
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from mcp.server.fastmcp import FastMCP

_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_TOKEN_SKEW_S = 300
_TIMEOUT_S = 30.0
_MAX_PER_PAGE = 100


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if required and not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _per_page(value: int) -> int:
    return max(1, min(int(value), _MAX_PER_PAGE))


def _repo(repo: str) -> str:
    target = repo.strip().removeprefix("https://github.com/").removesuffix(".git")
    parts = [p for p in target.split("/") if p]
    if len(parts) != 2:
        raise ValueError("repo must be 'owner/name'")
    return f"{parts[0]}/{parts[1]}"


def _content_b64(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdefABCDEF" for c in value)


@dataclass
class _CachedToken:
    value: str
    expires_at: float


class GitHubAppClient:
    """Small adapter over the GitHub REST API using App installation auth."""

    def __init__(self, *, http: httpx.Client | None = None) -> None:
        self._client_id = _env("GITHUB_APP_CLIENT_ID")
        self._private_key = _env("GITHUB_APP_PRIVATE_KEY")
        self._installation_id = _env("GITHUB_APP_INSTALLATION_ID")
        self._api_base = _env("GITHUB_API_BASE_URL", required=False, default=_API_BASE).rstrip("/")
        self._http = http or httpx.Client(base_url=self._api_base, timeout=_TIMEOUT_S)
        self._token: _CachedToken | None = None

    def _jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 540,
            "iss": self._client_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def _installation_token(self) -> str:
        now = time.time()
        if self._token and now < self._token.expires_at - _TOKEN_SKEW_S:
            return self._token.value
        response = self._http.post(
            f"/app/installations/{self._installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._jwt()}",
                "X-GitHub-Api-Version": _API_VERSION,
            },
        )
        response.raise_for_status()
        payload = response.json()
        token = payload["token"]
        expires_at = payload.get("expires_at") or ""
        try:
            expires_ts = calendar.timegm(time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            expires_ts = now + 3600
        self._token = _CachedToken(value=token, expires_at=expires_ts)
        return token

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._installation_token()}",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        response = self._http.request(method, path, headers=headers, **kwargs)
        if response.status_code == 204:
            return {}
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.json() if exc.response.content else {}
            message = body.get("message") if isinstance(body, dict) else None
            raise RuntimeError(
                f"GitHub API {method} {path} failed with HTTP "
                f"{exc.response.status_code}: {message or exc.response.text}"
            ) from exc
        return response.json()


_github_instance: GitHubAppClient | None = None


def _github() -> GitHubAppClient:
    global _github_instance
    if _github_instance is None:
        _github_instance = GitHubAppClient()
    return _github_instance


_mcp = FastMCP(
    "bytedesk-github",
    host=_env("HOST", required=False, default="0.0.0.0"),
    port=int(_env("PORT", required=False, default="8000")),
    streamable_http_path="/mcp",
    stateless_http=True,
)


@_mcp.tool()
def get_file_contents(repo: str, path: str, ref: str | None = None) -> dict[str, Any]:
    """Read a file from a repository."""
    params = {"ref": ref} if ref else None
    data = _github().request("GET", f"/repos/{_repo(repo)}/contents/{path}", params=params)
    encoded = (data.get("content") or "").replace("\n", "")
    text = base64.b64decode(encoded).decode("utf-8") if encoded else ""
    return {
        "path": data.get("path"),
        "sha": data.get("sha"),
        "encoding": data.get("encoding"),
        "content": text,
        "html_url": data.get("html_url"),
    }


@_mcp.tool()
def create_or_update_file(
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str,
    sha: str | None = None,
) -> dict[str, Any]:
    """Create or update a UTF-8 text file on a branch."""
    target = _repo(repo)
    existing_sha = sha
    if existing_sha is None:
        try:
            existing = _github().request(
                "GET",
                f"/repos/{target}/contents/{path}",
                params={"ref": branch},
            )
            existing_sha = existing.get("sha")
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
    payload: dict[str, Any] = {
        "message": message,
        "content": _content_b64(content),
        "branch": branch,
    }
    if existing_sha:
        payload["sha"] = existing_sha
    data = _github().request("PUT", f"/repos/{target}/contents/{path}", json=payload)
    return {
        "content": data.get("content"),
        "commit": data.get("commit"),
    }


@_mcp.tool()
def list_branches(repo: str, per_page: int = 100) -> list[dict[str, Any]]:
    """List repository branches."""
    return _github().request(
        "GET",
        f"/repos/{_repo(repo)}/branches",
        params={"per_page": _per_page(per_page)},
    )


@_mcp.tool()
def create_branch(repo: str, branch: str, from_ref: str | None = None) -> dict[str, Any]:
    """Create a branch from a ref, SHA, or the repository default branch."""
    target = _repo(repo)
    source = from_ref
    if not source:
        repo_info = _github().request("GET", f"/repos/{target}")
        source = repo_info.get("default_branch") or "main"
    source_text = str(source)
    if _is_sha(source_text):
        source_sha = source_text
    else:
        ref_name = source_text.removeprefix("refs/")
        if "/" not in ref_name:
            ref_name = f"heads/{ref_name}"
        ref = _github().request("GET", f"/repos/{target}/git/ref/{ref_name}")
        source_sha = ref["object"]["sha"]
    return _github().request(
        "POST",
        f"/repos/{target}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": source_sha},
    )


@_mcp.tool()
def create_pull_request(
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str | None = None,
    draft: bool = True,
) -> dict[str, Any]:
    """Open a pull request."""
    return _github().request(
        "POST",
        f"/repos/{_repo(repo)}/pulls",
        json={"title": title, "head": head, "base": base, "body": body or "", "draft": draft},
    )


@_mcp.tool()
def get_pull_request(repo: str, pull_number: int) -> dict[str, Any]:
    """Get a pull request."""
    return _github().request("GET", f"/repos/{_repo(repo)}/pulls/{int(pull_number)}")


@_mcp.tool()
def list_pull_requests(
    repo: str,
    state: str = "open",
    base: str | None = None,
    head: str | None = None,
    per_page: int = 30,
) -> list[dict[str, Any]]:
    """List pull requests."""
    params: dict[str, Any] = {"state": state, "per_page": _per_page(per_page)}
    if base:
        params["base"] = base
    if head:
        params["head"] = head
    return _github().request("GET", f"/repos/{_repo(repo)}/pulls", params=params)


@_mcp.tool()
def merge_pull_request(
    repo: str,
    pull_number: int,
    merge_method: str = "squash",
    commit_title: str | None = None,
    commit_message: str | None = None,
) -> dict[str, Any]:
    """Merge a pull request when repository branch protection allows it."""
    payload: dict[str, Any] = {"merge_method": merge_method}
    if commit_title:
        payload["commit_title"] = commit_title
    if commit_message:
        payload["commit_message"] = commit_message
    return _github().request(
        "PUT",
        f"/repos/{_repo(repo)}/pulls/{int(pull_number)}/merge",
        json=payload,
    )


@_mcp.tool()
def create_issue(
    repo: str,
    title: str,
    body: str | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Create a GitHub issue."""
    payload: dict[str, Any] = {"title": title, "body": body or ""}
    if labels:
        payload["labels"] = labels
    return _github().request("POST", f"/repos/{_repo(repo)}/issues", json=payload)


@_mcp.tool()
def list_issues(
    repo: str,
    state: str = "open",
    labels: str | None = None,
    per_page: int = 30,
) -> list[dict[str, Any]]:
    """List GitHub issues."""
    params: dict[str, Any] = {"state": state, "per_page": _per_page(per_page)}
    if labels:
        params["labels"] = labels
    return _github().request("GET", f"/repos/{_repo(repo)}/issues", params=params)


@_mcp.tool()
def add_issue_comment(repo: str, issue_number: int, body: str) -> dict[str, Any]:
    """Add a comment to an issue or pull request."""
    return _github().request(
        "POST",
        f"/repos/{_repo(repo)}/issues/{int(issue_number)}/comments",
        json={"body": body},
    )


@_mcp.tool()
def search_code(query: str, repo: str | None = None, per_page: int = 30) -> dict[str, Any]:
    """Search code. Pass repo='owner/name' to scope the search."""
    q = f"{query} repo:{_repo(repo)}" if repo else query
    return _github().request(
        "GET",
        "/search/code",
        params={"q": q, "per_page": _per_page(per_page)},
    )


@_mcp.tool()
def search_repositories(query: str, per_page: int = 30) -> dict[str, Any]:
    """Search repositories visible to the GitHub App installation."""
    return _github().request(
        "GET",
        "/search/repositories",
        params={"q": query, "per_page": _per_page(per_page)},
    )


@_mcp.tool()
def list_commits(repo: str, sha: str | None = None, per_page: int = 30) -> list[dict[str, Any]]:
    """List commits for a repository or branch."""
    params: dict[str, Any] = {"per_page": _per_page(per_page)}
    if sha:
        params["sha"] = sha
    return _github().request("GET", f"/repos/{_repo(repo)}/commits", params=params)


@_mcp.tool()
def get_commit(repo: str, ref: str) -> dict[str, Any]:
    """Get a commit by SHA or ref."""
    return _github().request("GET", f"/repos/{_repo(repo)}/commits/{ref}")


def main() -> None:
    _mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
