"""Native GitHub tool over the GitHub REST v3 API (BDP-2404, ADR-0143).

The agent-facing arm of the ``github-engineering-copilot`` integration blueprint
— gives coding agents **autonomous** GitHub access (a native builtin tool
bypasses the MCP approval gate, so an agent can inspect repos/PRs/issues/checks
and leave comments without a human-in-the-loop prompt on every call).

**Read + comment only.** This tool deliberately exposes inspection and a single
issue/PR comment write. It does NOT merge, push, create PRs, or perform any other
destructive write — those stay behind a richer ceremony.

**Adapter pattern (ADR-0008).** ``_GitHubClient`` is the internal facade that
adapts the GitHub REST v3 API to a small set of operations; ``BytedeskGitHubTool``
is the agent-facing dispatcher over it. Swapping the external API (or stubbing it
in tests) means replacing the adapter, not the tool.

**Never crash the turn.** Every failure mode returns a structured
``{"ok": false, "error": ...}`` result rather than raising:

- missing/empty token → ``{"ok": false, "error": "github_not_configured"}``
- no repo arg and no default repo → ``{"ok": false, "error": "github_repo_not_configured"}``
- a 4xx/5xx from GitHub → ``{"ok": false, "error": "github_http_error", "status": ...}``
- a network/transport blip → ``{"ok": false, "error": "github_request_failed"}``
- a bad/unknown op or argument → ``{"ok": false, "error": ...}``

**Credentials** are read from the omnigent secret backend (BDP-2303) via
``omnigent.onboarding.secrets.load_secret`` — ``GITHUB_TOKEN`` (then
``BYTEDESK_GITHUB_TOKEN``) for auth and ``GITHUB_REPO`` (form ``owner/name``) for
the default repository. Auth is a Bearer token. Secret **values** are never
logged or echoed back to the agent.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

import httpx

from bytedesk_omnigent.tools._http_adapter import HttpToolClient, first_secret
from omnigent.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.github.com"
_DEFAULT_PER_PAGE = 30
_DEFAULT_FILE_PER_PAGE = 100
_MAX_PER_PAGE = 100

#: Token secret names this tool resolves through the secret backend — the GitHub
#: name first, then the ByteDesk-namespaced fallback.
_SECRET_TOKEN = ("GITHUB_TOKEN", "BYTEDESK_GITHUB_TOKEN")
#: Default repository (``owner/name``) for ops that omit an explicit ``repo``.
_SECRET_REPO = "GITHUB_REPO"


class GitHubNotConfiguredError(RuntimeError):
    """Raised internally when no GitHub token is set/empty."""


class GitHubRepoNotConfiguredError(RuntimeError):
    """Raised internally when an op needs a repo but none is supplied/configured."""


class _GitHubClient(HttpToolClient):
    """Internal Adapter over the GitHub REST v3 API (ADR-0008).

    Resolves credentials lazily from the secret backend on first use. The httpx
    client is injectable so tests never touch the network.
    """

    def __init__(
        self,
        *,
        base_url: str = _BASE_URL,
        token: str | None = None,
        default_repo: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._default_repo = default_repo
        self._client = client  # injectable for tests; built lazily otherwise
        self._resolved = token is not None  # creds passed in directly (tests)

    def _resolve_credentials(self) -> None:
        if self._resolved:
            return
        self._token = first_secret(_SECRET_TOKEN)
        if self._default_repo is None:
            from omnigent.onboarding.secrets import load_secret

            self._default_repo = (load_secret(_SECRET_REPO) or "").strip() or None
        self._resolved = True

    def _require_configured(self) -> None:
        self._resolve_credentials()
        if not self._token:
            raise GitHubNotConfiguredError(_SECRET_TOKEN[0])

    def _resolve_repo(self, repo: str | None) -> str:
        self._resolve_credentials()
        target = (repo or self._default_repo or "").strip()
        if not target:
            raise GitHubRepoNotConfiguredError(_SECRET_REPO)
        return target

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_json(self, path: str, **kwargs: Any) -> Any:
        resp = self._request("GET", path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    # ── operations ────────────────────────────────────────────────────────────

    def get_repo(self, repo: str | None) -> dict[str, Any]:
        return self._get_json(f"/repos/{self._resolve_repo(repo)}")

    def list_prs(
        self, repo: str | None, state: str, per_page: int
    ) -> list[dict[str, Any]]:
        return self._get_json(
            f"/repos/{self._resolve_repo(repo)}/pulls",
            params={"state": state, "per_page": per_page},
        )

    def get_pr(self, repo: str | None, number: int) -> dict[str, Any]:
        data = self._get_json(f"/repos/{self._resolve_repo(repo)}/pulls/{number}")
        return {
            "number": data.get("number"),
            "title": data.get("title"),
            "state": data.get("state"),
            "merged": data.get("merged"),
            "mergeable": data.get("mergeable"),
            "head_sha": (data.get("head") or {}).get("sha"),
            "base_ref": (data.get("base") or {}).get("ref"),
            "html_url": data.get("html_url"),
        }

    def get_pr_files(
        self, repo: str | None, number: int, per_page: int
    ) -> list[dict[str, Any]]:
        files = self._get_json(
            f"/repos/{self._resolve_repo(repo)}/pulls/{number}/files",
            params={"per_page": per_page},
        )
        return [
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "patch": f.get("patch"),
            }
            for f in files
        ]

    def get_pr_checks(self, repo: str | None, number: int) -> dict[str, Any]:
        target = self._resolve_repo(repo)
        pr = self._get_json(f"/repos/{target}/pulls/{number}")
        sha = (pr.get("head") or {}).get("sha")
        data = self._get_json(f"/repos/{target}/commits/{sha}/check-runs")
        runs = data.get("check_runs", []) or []
        by_conclusion: dict[str, int] = {}
        out_runs: list[dict[str, Any]] = []
        for run in runs:
            conclusion = run.get("conclusion")
            key = conclusion or "pending"
            by_conclusion[key] = by_conclusion.get(key, 0) + 1
            out_runs.append(
                {
                    "name": run.get("name"),
                    "status": run.get("status"),
                    "conclusion": conclusion,
                }
            )
        overall = self._aggregate_overall(runs)
        return {
            "sha": sha,
            "total": len(runs),
            "by_conclusion": by_conclusion,
            "overall": overall,
            "runs": out_runs,
        }

    @staticmethod
    def _aggregate_overall(runs: list[dict[str, Any]]) -> str:
        """success only when every run completed successfully; failure if any run
        failed/errored; otherwise (incomplete or empty) pending."""
        if not runs:
            return "pending"
        failure_conclusions = {"failure", "timed_out", "cancelled", "action_required"}
        if any((r.get("conclusion") in failure_conclusions) for r in runs):
            return "failure"
        if all(
            r.get("status") == "completed" and r.get("conclusion") == "success"
            for r in runs
        ):
            return "success"
        return "pending"

    def get_issue(self, repo: str | None, number: int) -> dict[str, Any]:
        return self._get_json(f"/repos/{self._resolve_repo(repo)}/issues/{number}")

    def list_issues(
        self,
        repo: str | None,
        state: str,
        labels: str | None,
        per_page: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": state, "per_page": per_page}
        if labels:
            params["labels"] = labels
        return self._get_json(
            f"/repos/{self._resolve_repo(repo)}/issues", params=params
        )

    def search_issues(self, query: str, per_page: int) -> dict[str, Any]:
        data = self._get_json(
            "/search/issues", params={"q": query, "per_page": per_page}
        )
        items = [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "html_url": item.get("html_url"),
            }
            for item in data.get("items", [])
        ]
        return {"total_count": data.get("total_count"), "items": items}

    def list_commits(
        self, repo: str | None, sha: str | None, per_page: int
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": per_page}
        if sha:
            params["sha"] = sha
        return self._get_json(
            f"/repos/{self._resolve_repo(repo)}/commits", params=params
        )

    def get_file(
        self, repo: str | None, path: str, ref: str | None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if ref:
            params["ref"] = ref
        data = self._get_json(
            f"/repos/{self._resolve_repo(repo)}/contents/{path}", params=params
        )
        # A directory listing returns a list; only files carry decodable content.
        if isinstance(data, dict) and data.get("encoding") == "base64":
            decoded = _decode_base64_text(data.get("content") or "")
            if decoded is not None:
                # Replace the heavy base64 blob with the decoded text.
                data = {k: v for k, v in data.items() if k != "content"}
                data["content"] = decoded
                data["decoded"] = True
        return data

    def add_comment(self, repo: str | None, number: int, body: str) -> dict[str, Any]:
        resp = self._request(
            "POST",
            f"/repos/{self._resolve_repo(repo)}/issues/{number}/comments",
            json={"body": body},
        )
        resp.raise_for_status()
        data = resp.json()
        return {"id": data.get("id"), "html_url": data.get("html_url")}


def _decode_base64_text(content: str) -> str | None:
    """Decode a base64 file blob to UTF-8 text, or ``None`` if it is not valid
    UTF-8 (binary) or otherwise undecodable — the caller falls back to raw
    metadata so a binary/oversize file never crashes the turn."""
    try:
        raw = base64.b64decode(content)
        return raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


class BytedeskGitHubTool(Tool):
    """Autonomous GitHub access for coding agents (inspect repos/PRs/issues/checks + comment)."""

    def __init__(self, client: _GitHubClient | None = None) -> None:
        self._github = client or _GitHubClient()

    @classmethod
    def name(cls) -> str:
        return "bytedesk_github"

    @classmethod
    def description(cls) -> str:
        return (
            "Inspect GitHub directly: read a repo, list/read pull requests, read "
            "PR files and CI check status, read/list/search issues, list commits, "
            "read a file, and add an issue/PR comment. Read + comment only — no "
            "merge, push, or PR creation. Use this to triage tickets, review PRs "
            "and CI, and leave notes — no human approval prompt. Pick the "
            "operation with 'op'. Repo defaults to GITHUB_REPO; override with 'repo'."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "bytedesk_github",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": [
                                "get_repo",
                                "list_prs",
                                "get_pr",
                                "get_pr_files",
                                "get_pr_checks",
                                "get_issue",
                                "list_issues",
                                "search_issues",
                                "list_commits",
                                "get_file",
                                "add_comment",
                            ],
                            "description": "Which GitHub operation to perform.",
                        },
                        "repo": {
                            "type": "string",
                            "description": (
                                "Repository as 'owner/name'. Defaults to GITHUB_REPO "
                                "when omitted (all ops except search_issues)."
                            ),
                        },
                        "number": {
                            "type": "integer",
                            "description": (
                                "PR or issue number "
                                "(op=get_pr/get_pr_files/get_pr_checks/get_issue/add_comment)."
                            ),
                        },
                        "state": {
                            "type": "string",
                            "description": (
                                "Filter state, e.g. 'open'/'closed'/'all' "
                                "(op=list_prs/list_issues, default 'open')."
                            ),
                            "default": "open",
                        },
                        "labels": {
                            "type": "string",
                            "description": (
                                "Comma-separated label filter (op=list_issues, optional)."
                            ),
                        },
                        "query": {
                            "type": "string",
                            "description": (
                                "GitHub issue/PR search query, e.g. "
                                "'repo:acme/widget is:open' (op=search_issues)."
                            ),
                        },
                        "sha": {
                            "type": "string",
                            "description": (
                                "Branch/tag/commit SHA to list commits from "
                                "(op=list_commits, optional)."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": "File path within the repo (op=get_file).",
                        },
                        "ref": {
                            "type": "string",
                            "description": (
                                "Branch/tag/commit to read the file at "
                                "(op=get_file, optional)."
                            ),
                        },
                        "per_page": {
                            "type": "integer",
                            "description": (
                                "Page size (op=list_prs/get_pr_files/list_issues/"
                                "search_issues/list_commits). Defaults to 30 (100 for "
                                "get_pr_files)."
                            ),
                        },
                        "body": {
                            "type": "string",
                            "description": (
                                "Comment text (op=add_comment). Works on both issues "
                                "and PRs."
                            ),
                        },
                    },
                    "required": ["op"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del ctx  # GitHub identity is the configured token, not the agent.
        try:
            args: dict[str, Any] = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "invalid_arguments_json"})

        op = args.get("op")
        try:
            result = self._dispatch(op, args)
        except GitHubNotConfiguredError:
            return json.dumps({"ok": False, "error": "github_not_configured"})
        except GitHubRepoNotConfiguredError:
            return json.dumps({"ok": False, "error": "github_repo_not_configured"})
        except httpx.HTTPStatusError as exc:
            # A 4xx/5xx from GitHub — surface the status, never raise.
            logger.warning("github %s returned HTTP %s", op, exc.response.status_code)
            return json.dumps(
                {
                    "ok": False,
                    "error": "github_http_error",
                    "status": exc.response.status_code,
                }
            )
        except httpx.HTTPError as exc:
            # Transport/network blip — log the type, never the credentials.
            logger.warning("github %s request failed: %s", op, type(exc).__name__)
            return json.dumps({"ok": False, "error": "github_request_failed"})
        return json.dumps(result)

    @staticmethod
    def _per_page(args: dict[str, Any], default: int) -> int:
        try:
            value = int(args.get("per_page", default))
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, _MAX_PER_PAGE))

    @staticmethod
    def _number(args: dict[str, Any]) -> int | None:
        try:
            return int(args["number"])
        except (KeyError, TypeError, ValueError):
            return None

    def _dispatch(self, op: Any, args: dict[str, Any]) -> dict[str, Any]:
        repo = args.get("repo")

        if op == "get_repo":
            return {"ok": True, "repo": self._github.get_repo(repo)}

        if op == "list_prs":
            return {
                "ok": True,
                "pull_requests": self._github.list_prs(
                    repo,
                    str(args.get("state") or "open"),
                    self._per_page(args, _DEFAULT_PER_PAGE),
                ),
            }

        if op == "get_pr":
            number = self._number(args)
            if number is None:
                return {"ok": False, "error": "missing required 'number'"}
            return {"ok": True, "pr": self._github.get_pr(repo, number)}

        if op == "get_pr_files":
            number = self._number(args)
            if number is None:
                return {"ok": False, "error": "missing required 'number'"}
            return {
                "ok": True,
                "files": self._github.get_pr_files(
                    repo, number, self._per_page(args, _DEFAULT_FILE_PER_PAGE)
                ),
            }

        if op == "get_pr_checks":
            number = self._number(args)
            if number is None:
                return {"ok": False, "error": "missing required 'number'"}
            return {"ok": True, "checks": self._github.get_pr_checks(repo, number)}

        if op == "get_issue":
            number = self._number(args)
            if number is None:
                return {"ok": False, "error": "missing required 'number'"}
            return {"ok": True, "issue": self._github.get_issue(repo, number)}

        if op == "list_issues":
            return {
                "ok": True,
                "issues": self._github.list_issues(
                    repo,
                    str(args.get("state") or "open"),
                    args.get("labels"),
                    self._per_page(args, _DEFAULT_PER_PAGE),
                ),
            }

        if op == "search_issues":
            query = args.get("query")
            if not query:
                return {"ok": False, "error": "missing required 'query'"}
            return {
                "ok": True,
                **self._github.search_issues(
                    query, self._per_page(args, _DEFAULT_PER_PAGE)
                ),
            }

        if op == "list_commits":
            return {
                "ok": True,
                "commits": self._github.list_commits(
                    repo, args.get("sha"), self._per_page(args, _DEFAULT_PER_PAGE)
                ),
            }

        if op == "get_file":
            path = args.get("path")
            if not path:
                return {"ok": False, "error": "missing required 'path'"}
            return {
                "ok": True,
                "file": self._github.get_file(repo, path, args.get("ref")),
            }

        if op == "add_comment":
            number = self._number(args)
            body = args.get("body")
            if number is None or not body:
                return {"ok": False, "error": "missing required 'number' or 'body'"}
            return {
                "ok": True,
                "comment": self._github.add_comment(repo, number, body),
            }

        return {"ok": False, "error": f"unknown op {op!r}"}
