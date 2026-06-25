"""Native Jira tool over the Atlassian Cloud REST v3 API (BDP-2402, ADR-0143).

The agent-facing arm of the ``linear-jira-work-intake`` integration blueprint —
gives team agents **autonomous** Jira access (a native builtin tool bypasses the
MCP approval gate, so an agent can triage/search/comment/transition/create
without a human-in-the-loop prompt on every call).

**Adapter pattern (ADR-0008).** ``_JiraClient`` is the internal facade that
adapts Atlassian Cloud REST v3 to a small set of operations; ``BytedeskJiraTool``
is the agent-facing dispatcher over it. Swapping the external API (or stubbing it
in tests) means replacing the adapter, not the tool.

**Never crash the turn.** Every failure mode returns a structured
``{"ok": false, "error": ...}`` result rather than raising:

- missing/empty credentials → ``{"ok": false, "error": "jira_not_configured"}``
- a 4xx/5xx from Jira → ``{"ok": false, "error": "jira_http_error", "status": ...}``
- a network/transport blip → ``{"ok": false, "error": "jira_request_failed"}``
- a bad/unknown op or argument → ``{"ok": false, "error": ...}``

**Credentials** are read from the omnigent secret backend (BDP-2303) via
``omnigent.onboarding.secrets.load_secret`` — ``JIRA_BASE_URL`` /
``JIRA_EMAIL`` / ``JIRA_API_TOKEN``. Auth is HTTP Basic ``email:api_token``
(base64). Secret **values** are never logged or echoed back to the agent.

Comments and issue descriptions use **ADF** (Atlassian Document Format); plain
text supplied by the agent is wrapped in the minimal ADF doc shape.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from omnigent.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

_TIMEOUT_S = 20.0
_DEFAULT_MAX_RESULTS = 20
_MAX_MAX_RESULTS = 100

#: The three secret names this tool resolves through the secret backend.
_SECRET_BASE_URL = "JIRA_BASE_URL"
_SECRET_EMAIL = "JIRA_EMAIL"
_SECRET_API_TOKEN = "JIRA_API_TOKEN"


def _adf_doc(text: str) -> dict[str, Any]:
    """Wrap a plain string in the minimal Atlassian Document Format doc shape.

    A blank/whitespace body still produces a valid (empty-paragraph) document so
    the Jira API does not reject it.
    """
    content: list[dict[str, Any]] = [{"type": "paragraph", "content": []}]
    if text:
        content = [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ]
    return {"type": "doc", "version": 1, "content": content}


class JiraNotConfiguredError(RuntimeError):
    """Raised internally when one of the three Jira secrets is unset/empty."""


class _JiraClient:
    """Internal Adapter over Atlassian Cloud REST v3 (ADR-0008).

    Resolves credentials lazily from the secret backend on first use. The httpx
    client is injectable so tests never touch the network.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url
        self._email = email
        self._api_token = api_token
        self._client = client  # injectable for tests; built lazily otherwise
        self._resolved = base_url is not None  # creds passed in directly (tests)

    def _resolve_credentials(self) -> None:
        if self._resolved:
            return
        from omnigent.onboarding.secrets import load_secret

        self._base_url = (load_secret(_SECRET_BASE_URL) or "").strip().rstrip("/")
        self._email = (load_secret(_SECRET_EMAIL) or "").strip()
        self._api_token = (load_secret(_SECRET_API_TOKEN) or "").strip()
        self._resolved = True

    def _require_configured(self) -> None:
        self._resolve_credentials()
        if not self._base_url or not self._email or not self._api_token:
            raise JiraNotConfiguredError(_SECRET_API_TOKEN)

    def _auth_header(self) -> str:
        token = base64.b64encode(f"{self._email}:{self._api_token}".encode()).decode()
        return f"Basic {token}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self._base_url, timeout=_TIMEOUT_S)
        return self._client

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._require_configured()
        return self._http().request(
            method, path, headers=self._headers(), **kwargs
        )

    # ── operations ────────────────────────────────────────────────────────────

    def search(self, jql: str, max_results: int) -> list[dict[str, Any]]:
        resp = self._request(
            "POST",
            "/rest/api/3/search/jql",
            json={
                "jql": jql,
                "maxResults": max_results,
                "fields": ["summary", "status", "assignee"],
            },
        )
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        out: list[dict[str, Any]] = []
        for issue in issues:
            fields = issue.get("fields") or {}
            status = (fields.get("status") or {}).get("name")
            assignee_obj = fields.get("assignee") or {}
            assignee = assignee_obj.get("displayName") or assignee_obj.get("emailAddress")
            out.append(
                {
                    "key": issue.get("key"),
                    "summary": fields.get("summary"),
                    "status": status,
                    "assignee": assignee,
                }
            )
        return out

    def get_issue(self, key: str) -> dict[str, Any]:
        resp = self._request("GET", f"/rest/api/3/issue/{key}")
        resp.raise_for_status()
        return resp.json()

    def add_comment(self, key: str, body: str) -> dict[str, Any]:
        resp = self._request(
            "POST",
            f"/rest/api/3/issue/{key}/comment",
            json={"body": _adf_doc(body)},
        )
        resp.raise_for_status()
        data = resp.json()
        return {"id": data.get("id")}

    def transition(self, key: str, transition_name_or_id: str) -> dict[str, Any]:
        # Resolve the available transitions, then match by id or (case-insensitive) name.
        resp = self._request("GET", f"/rest/api/3/issue/{key}/transitions")
        resp.raise_for_status()
        transitions = resp.json().get("transitions", [])
        target = str(transition_name_or_id).strip()
        target_lower = target.lower()
        match = None
        for tr in transitions:
            if str(tr.get("id")) == target or str(tr.get("name", "")).lower() == target_lower:
                match = tr
                break
        if match is None:
            available = [tr.get("name") for tr in transitions]
            return {
                "ok": False,
                "error": "transition_not_found",
                "requested": transition_name_or_id,
                "available": available,
            }
        post = self._request(
            "POST",
            f"/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": str(match.get("id"))}},
        )
        post.raise_for_status()
        return {
            "ok": True,
            "key": key,
            "transition": match.get("name"),
            "transition_id": str(match.get("id")),
        }

    def create_issue(
        self,
        *,
        project_key: str,
        summary: str,
        description: str,
        issue_type: str,
        parent: str | None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "description": _adf_doc(description),
            "issuetype": {"name": issue_type},
        }
        if parent:
            fields["parent"] = {"key": parent}
        resp = self._request("POST", "/rest/api/3/issue", json={"fields": fields})
        resp.raise_for_status()
        data = resp.json()
        return {"key": data.get("key"), "id": data.get("id")}


class BytedeskJiraTool(Tool):
    """Autonomous Jira access for team agents (search / read / comment / transition / create)."""

    def __init__(self, client: _JiraClient | None = None) -> None:
        self._jira = client or _JiraClient()

    @classmethod
    def name(cls) -> str:
        return "bytedesk_jira"

    @classmethod
    def description(cls) -> str:
        return (
            "Work with Jira issues directly: search by JQL, read an issue, add a "
            "comment, transition status, or create an issue. Use this to triage "
            "tickets, leave progress notes, move work through the board, and file "
            "follow-ups — no human approval prompt. Pick the operation with 'op'."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "bytedesk_jira",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": [
                                "search",
                                "get_issue",
                                "add_comment",
                                "transition",
                                "create_issue",
                            ],
                            "description": "Which Jira operation to perform.",
                        },
                        "jql": {
                            "type": "string",
                            "description": "JQL query (op=search).",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": (
                                f"Max search hits (op=search, default {_DEFAULT_MAX_RESULTS})."
                            ),
                            "default": _DEFAULT_MAX_RESULTS,
                        },
                        "key": {
                            "type": "string",
                            "description": (
                                "Issue key, e.g. 'BDP-123' "
                                "(op=get_issue/add_comment/transition)."
                            ),
                        },
                        "body": {
                            "type": "string",
                            "description": "Comment text, plain (op=add_comment). Wrapped in ADF.",
                        },
                        "transition_name_or_id": {
                            "type": "string",
                            "description": (
                                "Target transition name or id, e.g. 'In Progress' "
                                "(op=transition)."
                            ),
                        },
                        "project_key": {
                            "type": "string",
                            "description": (
                                "Project key for the new issue, e.g. 'BDP' (op=create_issue)."
                            ),
                        },
                        "summary": {
                            "type": "string",
                            "description": "New issue summary (op=create_issue).",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "New issue description, plain (op=create_issue). Wrapped in ADF."
                            ),
                        },
                        "issue_type": {
                            "type": "string",
                            "description": "New issue type (op=create_issue, default 'Task').",
                            "default": "Task",
                        },
                        "parent": {
                            "type": "string",
                            "description": (
                                "Parent issue key for a subtask/child "
                                "(op=create_issue, optional)."
                            ),
                        },
                    },
                    "required": ["op"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del ctx  # Jira identity is the configured service account, not the agent.
        try:
            args: dict[str, Any] = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "invalid_arguments_json"})

        op = args.get("op")
        try:
            result = self._dispatch(op, args)
        except JiraNotConfiguredError:
            return json.dumps({"ok": False, "error": "jira_not_configured"})
        except httpx.HTTPStatusError as exc:
            # A 4xx/5xx from Jira — surface the status, never raise.
            logger.warning("jira %s returned HTTP %s", op, exc.response.status_code)
            return json.dumps(
                {
                    "ok": False,
                    "error": "jira_http_error",
                    "status": exc.response.status_code,
                }
            )
        except httpx.HTTPError as exc:
            # Transport/network blip — log the type, never the credentials.
            logger.warning("jira %s request failed: %s", op, type(exc).__name__)
            return json.dumps({"ok": False, "error": "jira_request_failed"})
        return json.dumps(result)

    def _dispatch(self, op: Any, args: dict[str, Any]) -> dict[str, Any]:
        if op == "search":
            jql = args.get("jql")
            if not jql:
                return {"ok": False, "error": "missing required 'jql'"}
            try:
                max_results = int(args.get("max_results", _DEFAULT_MAX_RESULTS))
            except (TypeError, ValueError):
                max_results = _DEFAULT_MAX_RESULTS
            max_results = max(1, min(max_results, _MAX_MAX_RESULTS))
            return {"ok": True, "issues": self._jira.search(jql, max_results)}

        if op == "get_issue":
            key = args.get("key")
            if not key:
                return {"ok": False, "error": "missing required 'key'"}
            return {"ok": True, "issue": self._jira.get_issue(key)}

        if op == "add_comment":
            key = args.get("key")
            body = args.get("body")
            if not key or not body:
                return {"ok": False, "error": "missing required 'key' or 'body'"}
            return {"ok": True, "comment": self._jira.add_comment(key, body)}

        if op == "transition":
            key = args.get("key")
            target = args.get("transition_name_or_id")
            if not key or not target:
                return {
                    "ok": False,
                    "error": "missing required 'key' or 'transition_name_or_id'",
                }
            return self._jira.transition(key, target)

        if op == "create_issue":
            project_key = args.get("project_key")
            summary = args.get("summary")
            if not project_key or not summary:
                return {"ok": False, "error": "missing required 'project_key' or 'summary'"}
            return {
                "ok": True,
                "created": self._jira.create_issue(
                    project_key=project_key,
                    summary=summary,
                    description=args.get("description", ""),
                    issue_type=args.get("issue_type") or "Task",
                    parent=args.get("parent"),
                ),
            }

        return {"ok": False, "error": f"unknown op {op!r}"}
