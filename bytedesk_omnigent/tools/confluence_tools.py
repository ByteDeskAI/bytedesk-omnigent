"""Native Confluence tool over the Atlassian Cloud REST API (BDP-2403, ADR-0143).

The agent-facing arm of the ``confluence-knowledge-operator`` integration
blueprint — gives team agents **autonomous** Confluence access (a native builtin
tool bypasses the MCP approval gate, so an agent can search/read/create/update/
comment without a human-in-the-loop prompt on every call).

**Adapter pattern (ADR-0008).** ``_ConfluenceClient`` is the internal facade that
adapts the Confluence Cloud REST API (v1 CQL search + v2 pages/comments) to a
small set of operations; ``BytedeskConfluenceTool`` is the agent-facing dispatcher
over it. Swapping the external API (or stubbing it in tests) means replacing the
adapter, not the tool.

**Never crash the turn.** Every failure mode returns a structured
``{"ok": false, "error": ...}`` result rather than raising:

- missing/empty credentials → ``{"ok": false, "error": "confluence_not_configured"}``
- a 4xx/5xx from Confluence → ``{"ok": false, "error": "confluence_http_error", "status": ...}``
- a network/transport blip → ``{"ok": false, "error": "confluence_request_failed"}``
- a bad/unknown op or argument → ``{"ok": false, "error": ...}``

**Credentials** are read from the omnigent secret backend (BDP-2303) via
``omnigent.onboarding.secrets.load_secret``. It shares the Atlassian account with
``bytedesk_jira`` — Confluence-specific names take precedence, then the Jira
names: ``CONFLUENCE_BASE_URL`` → ``JIRA_BASE_URL`` (the bare
``https://<site>.atlassian.net`` site root; the client appends ``/wiki/...``),
``ATLASSIAN_EMAIL`` → ``JIRA_EMAIL``, ``ATLASSIAN_API_TOKEN`` → ``JIRA_API_TOKEN``.
Auth is HTTP Basic ``email:api_token`` (base64). Secret **values** are never
logged or echoed back to the agent.

Page bodies and comments use Confluence **storage format** (XHTML); plain text
supplied by the agent is wrapped in a ``<p>...</p>`` element.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from bytedesk_omnigent.tools._http_adapter import HttpToolClient, first_secret
from omnigent.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 250

#: Secret names this tool resolves through the secret backend — Confluence first,
#: then the shared Jira/Atlassian names (one Atlassian account, zero new secrets).
_SECRET_BASE_URL = ("CONFLUENCE_BASE_URL", "JIRA_BASE_URL")
_SECRET_EMAIL = ("ATLASSIAN_EMAIL", "JIRA_EMAIL")
_SECRET_API_TOKEN = ("ATLASSIAN_API_TOKEN", "JIRA_API_TOKEN")


def _storage_body(text: str) -> str:
    """Render an agent-supplied body as a Confluence storage-format XHTML string.

    Plain text is wrapped in a paragraph element; a value that already looks like
    markup (starts with ``<``) is passed through untouched. A blank/whitespace
    body still produces a valid (empty-paragraph) document.
    """
    value = (text or "").strip()
    if value.startswith("<"):
        return text
    return f"<p>{value}</p>"


class ConfluenceNotConfiguredError(RuntimeError):
    """Raised internally when one of the three Confluence secrets is unset/empty."""


class _ConfluenceClient(HttpToolClient):
    """Internal Adapter over the Confluence Cloud REST API (ADR-0008).

    Resolves credentials lazily from the secret backend on first use. The httpx
    client is injectable so tests never touch the network. The base URL is the
    bare site root; this adapter appends the ``/wiki/...`` Confluence paths.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        connection_id: str | None = None,
        headers: dict[str, str] | None = None,
        path_prefix: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url
        self._email = email
        self._api_token = api_token
        self._connection_id = connection_id
        self._headers_override = headers
        self._path_prefix = path_prefix.rstrip("/")
        self._client = client  # injectable for tests; built lazily otherwise
        self._resolved = base_url is not None  # creds passed in directly (tests)

    def _resolve_credentials(self) -> None:
        if self._resolved:
            return
        if self._connection_id:
            from bytedesk_omnigent.connectors.credentials import resolve_atlassian_credentials

            creds = resolve_atlassian_credentials(self._connection_id, service="confluence")
            self._base_url = creds.base_url
            self._path_prefix = creds.path_prefix.rstrip("/")
            self._headers_override = creds.headers
            self._resolved = True
            return
        base = first_secret(_SECRET_BASE_URL).rstrip("/")
        # The site root may be supplied with a trailing /wiki; strip it so the
        # adapter's own /wiki/... paths compose cleanly.
        if base.endswith("/wiki"):
            base = base[: -len("/wiki")]
        self._base_url = base.rstrip("/")
        self._email = first_secret(_SECRET_EMAIL)
        self._api_token = first_secret(_SECRET_API_TOKEN)
        self._resolved = True

    def _require_configured(self) -> None:
        self._resolve_credentials()
        if self._headers_override:
            if not self._base_url:
                raise ConfluenceNotConfiguredError(_SECRET_BASE_URL[0])
            return
        if not self._base_url or not self._email or not self._api_token:
            raise ConfluenceNotConfiguredError(_SECRET_API_TOKEN[0])

    def _auth_header(self) -> str:
        token = base64.b64encode(f"{self._email}:{self._api_token}".encode()).decode()
        return f"Basic {token}"

    def _headers(self) -> dict[str, str]:
        if self._headers_override is not None:
            return dict(self._headers_override)
        return {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._require_configured()
        if self._path_prefix:
            path = f"{self._path_prefix}{path}"
        return super()._request(method, path, **kwargs)

    # ── operations ────────────────────────────────────────────────────────────

    def search(self, cql: str, limit: int) -> list[dict[str, Any]]:
        resp = self._request(
            "GET",
            "/wiki/rest/api/content/search",
            params={"cql": cql, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    def get_page(self, page_id: str) -> dict[str, Any]:
        data = self._get_page_raw(page_id)
        return {
            "id": data.get("id"),
            "title": data.get("title"),
            "version": (data.get("version") or {}).get("number"),
            "body": ((data.get("body") or {}).get("storage") or {}).get("value"),
        }

    def _get_page_raw(self, page_id: str) -> dict[str, Any]:
        resp = self._request(
            "GET",
            f"/wiki/api/v2/pages/{page_id}",
            params={"body-format": "storage"},
        )
        resp.raise_for_status()
        return resp.json()

    def _resolve_space_id(self, space_key: str) -> str | None:
        resp = self._request(
            "GET", "/wiki/api/v2/spaces", params={"keys": space_key}
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        return results[0].get("id")

    def create_page(
        self,
        *,
        space_id: str,
        title: str,
        body: str,
        parent_id: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": _storage_body(body)},
        }
        if parent_id:
            payload["parentId"] = parent_id
        resp = self._request("POST", "/wiki/api/v2/pages", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return {"id": data.get("id"), "title": data.get("title")}

    def update_page(
        self,
        *,
        page_id: str,
        title: str,
        body: str,
        version: int | None,
    ) -> dict[str, Any]:
        if version is None:
            current = self._get_page_raw(page_id)
            version = int((current.get("version") or {}).get("number") or 0)
        next_version = int(version) + 1
        payload = {
            "id": page_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": _storage_body(body)},
            "version": {"number": next_version},
        }
        resp = self._request("PUT", f"/wiki/api/v2/pages/{page_id}", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return {
            "id": data.get("id"),
            "version": (data.get("version") or {}).get("number"),
        }

    def add_comment(self, page_id: str, body: str) -> dict[str, Any]:
        resp = self._request(
            "POST",
            "/wiki/api/v2/footer-comments",
            json={
                "pageId": page_id,
                "body": {"representation": "storage", "value": _storage_body(body)},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {"id": data.get("id")}


class BytedeskConfluenceTool(Tool):
    """Autonomous Confluence access for team agents (search / read / create / update / comment)."""

    def __init__(self, client: _ConfluenceClient | None = None) -> None:
        self._confluence = client or _ConfluenceClient()

    @classmethod
    def from_config(
        cls, config: dict[str, str] | None = None
    ) -> BytedeskConfluenceTool:
        config = config if isinstance(config, dict) else {}
        connection_id = config.get("connection_id") or None
        return cls(client=_ConfluenceClient(connection_id=connection_id))

    @classmethod
    def name(cls) -> str:
        return "bytedesk_confluence"

    @classmethod
    def description(cls) -> str:
        return (
            "Work with Confluence pages directly: search by CQL, read a page, "
            "create a page, update a page, or add a comment. Use this to read and "
            "write the team knowledge base — runbooks, plans, notes, status — no "
            "human approval prompt. Pick the operation with 'op'."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "bytedesk_confluence",
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": [
                                "search",
                                "get_page",
                                "create_page",
                                "update_page",
                                "add_comment",
                            ],
                            "description": "Which Confluence operation to perform.",
                        },
                        "cql": {
                            "type": "string",
                            "description": (
                                "CQL query, e.g. \"type=page AND space=BDP\" (op=search)."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"Max search hits (op=search, default {_DEFAULT_LIMIT})."
                            ),
                            "default": _DEFAULT_LIMIT,
                        },
                        "page_id": {
                            "type": "string",
                            "description": (
                                "Page id (op=get_page/update_page/add_comment)."
                            ),
                        },
                        "space_id": {
                            "type": "string",
                            "description": (
                                "Numeric space id for a new page (op=create_page). "
                                "Use this or 'space_key'."
                            ),
                        },
                        "space_key": {
                            "type": "string",
                            "description": (
                                "Space key for a new page, e.g. 'BDP' — resolved to an "
                                "id (op=create_page). Use this or 'space_id'."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": "Page title (op=create_page/update_page).",
                        },
                        "body": {
                            "type": "string",
                            "description": (
                                "Page/comment body. Plain text is wrapped as storage "
                                "XHTML; existing markup is kept as-is "
                                "(op=create_page/update_page/add_comment)."
                            ),
                        },
                        "parent_id": {
                            "type": "string",
                            "description": (
                                "Parent page id for a child page "
                                "(op=create_page, optional)."
                            ),
                        },
                        "version": {
                            "type": "integer",
                            "description": (
                                "Current version number (op=update_page, optional). "
                                "Omit to read + increment it automatically."
                            ),
                        },
                    },
                    "required": ["op"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del ctx  # Confluence identity is the configured service account, not the agent.
        try:
            args: dict[str, Any] = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "invalid_arguments_json"})

        op = args.get("op")
        try:
            result = self._dispatch(op, args)
        except ConfluenceNotConfiguredError:
            return json.dumps({"ok": False, "error": "confluence_not_configured"})
        except httpx.HTTPStatusError as exc:
            # A 4xx/5xx from Confluence — surface the status, never raise.
            logger.warning(
                "confluence %s returned HTTP %s", op, exc.response.status_code
            )
            return json.dumps(
                {
                    "ok": False,
                    "error": "confluence_http_error",
                    "status": exc.response.status_code,
                }
            )
        except httpx.HTTPError as exc:
            # Transport/network blip — log the type, never the credentials.
            logger.warning("confluence %s request failed: %s", op, type(exc).__name__)
            return json.dumps({"ok": False, "error": "confluence_request_failed"})
        return json.dumps(result)

    def _dispatch(self, op: Any, args: dict[str, Any]) -> dict[str, Any]:
        if op == "search":
            cql = args.get("cql")
            if not cql:
                return {"ok": False, "error": "missing required 'cql'"}
            try:
                limit = int(args.get("limit", _DEFAULT_LIMIT))
            except (TypeError, ValueError):
                limit = _DEFAULT_LIMIT
            limit = max(1, min(limit, _MAX_LIMIT))
            return {"ok": True, "results": self._confluence.search(cql, limit)}

        if op == "get_page":
            page_id = args.get("page_id")
            if not page_id:
                return {"ok": False, "error": "missing required 'page_id'"}
            return {"ok": True, "page": self._confluence.get_page(page_id)}

        if op == "create_page":
            space_id = args.get("space_id")
            space_key = args.get("space_key")
            title = args.get("title")
            if (not space_id and not space_key) or not title:
                return {
                    "ok": False,
                    "error": "missing required 'space_id' (or 'space_key') or 'title'",
                }
            if not space_id:
                space_id = self._confluence._resolve_space_id(space_key)
                if not space_id:
                    return {
                        "ok": False,
                        "error": "space_key_not_found",
                        "requested": space_key,
                    }
            return {
                "ok": True,
                "page": self._confluence.create_page(
                    space_id=space_id,
                    title=title,
                    body=args.get("body", ""),
                    parent_id=args.get("parent_id"),
                ),
            }

        if op == "update_page":
            page_id = args.get("page_id")
            title = args.get("title")
            if not page_id or not title:
                return {"ok": False, "error": "missing required 'page_id' or 'title'"}
            version = args.get("version")
            if version is not None:
                try:
                    version = int(version)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "invalid 'version'"}
            return {
                "ok": True,
                "page": self._confluence.update_page(
                    page_id=page_id,
                    title=title,
                    body=args.get("body", ""),
                    version=version,
                ),
            }

        if op == "add_comment":
            page_id = args.get("page_id")
            body = args.get("body")
            if not page_id or not body:
                return {"ok": False, "error": "missing required 'page_id' or 'body'"}
            return {"ok": True, "comment": self._confluence.add_comment(page_id, body)}

        return {"ok": False, "error": f"unknown op {op!r}"}
