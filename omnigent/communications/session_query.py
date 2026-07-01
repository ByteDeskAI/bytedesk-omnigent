"""Session query service for runner-backed communication tools."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from omnigent.session_lifecycle import is_session_closed

_logger = logging.getLogger(__name__)


class SessionQueryService:
    """REST-backed session query adapter used by runner ``sys_session_*`` tools."""

    def __init__(
        self,
        server_client: httpx.AsyncClient,
        *,
        agent_list_page_limit: int = 1000,
    ) -> None:
        self._server_client = server_client
        self._agent_list_page_limit = agent_list_page_limit

    async def runner_online_or_none(self, runner_id: str | None) -> bool | None:
        """Resolve a runner's live connectivity."""
        if not runner_id:
            return None
        try:
            resp = await self._server_client.get(f"/v1/runners/{runner_id}/status", timeout=30.0)
        except Exception:  # noqa: BLE001
            return None
        if resp.status_code != 200:
            return None
        online = resp.json().get("online")
        return online if isinstance(online, bool) else None

    async def session_parent_id(self, conversation_id: str) -> str | None:
        """Return a session's parent id, if available."""
        try:
            snap = await self._server_client.get(f"/v1/sessions/{conversation_id}", timeout=30.0)
        except Exception:  # noqa: BLE001
            return None
        if snap.status_code != 200:
            return None
        parent = snap.json().get("parent_session_id")
        return parent if isinstance(parent, str) and parent else None

    async def list_sessions(self, conversation_id: str, agent_name: Any = None) -> str:
        """Return the two-view session list: sub-agents plus global sessions."""
        sub_agents = await self.collect_sub_agents(conversation_id)
        sessions = await self.collect_global_sessions(agent_name)
        return json.dumps({"sub_agents": sub_agents, "sessions": sessions})

    async def collect_sub_agents(
        self,
        conversation_id: str,
    ) -> list[dict[str, str | None]]:
        """Collect the caller's named sub-agent view."""
        try:
            resp = await self._server_client.get(
                f"/v1/sessions/{conversation_id}/child_sessions",
                params={"limit": 100},
                timeout=30.0,
            )
        except Exception:  # noqa: BLE001
            return []
        if resp.status_code != 200:
            return []
        result = self._child_rows_to_entries(resp.json().get("data", []))

        parent_id = await self.session_parent_id(conversation_id)
        if parent_id is not None:
            result.append({"agent": "main", "title": None, "conversation_id": parent_id})
            try:
                sib_resp = await self._server_client.get(
                    f"/v1/sessions/{parent_id}/child_sessions",
                    params={"limit": 100},
                    timeout=30.0,
                )
                if sib_resp.status_code == 200:
                    for entry in self._child_rows_to_entries(sib_resp.json().get("data", [])):
                        if entry["conversation_id"] != conversation_id:
                            result.append(entry)
            except Exception:  # noqa: BLE001
                _logger.debug(
                    "sys_session_list sibling enrichment failed for parent %s",
                    parent_id,
                    exc_info=True,
                )
        return result

    async def collect_global_sessions(self, agent_name: Any) -> list[dict[str, Any]]:
        """Fetch the global session list, annotated with runner connectivity."""
        params: dict[str, Any] = {"limit": self._agent_list_page_limit, "order": "desc"}
        if isinstance(agent_name, str) and agent_name:
            params["agent_name"] = agent_name
        try:
            resp = await self._server_client.get("/v1/sessions", params=params, timeout=30.0)
        except Exception:  # noqa: BLE001
            return []
        if resp.status_code != 200:
            return []
        rows = resp.json().get("data", [])
        if not isinstance(rows, list):
            return []
        online = await self.resolve_runner_online_map(rows)
        return [
            {
                "session_id": r.get("id"),
                "agent_name": r.get("agent_name"),
                "title": r.get("title"),
                "status": r.get("status"),
                "runner_id": r.get("runner_id"),
                "runner_online": online.get(r.get("runner_id")),
                "parent_session_id": r.get("parent_session_id"),
            }
            for r in rows
        ]

    async def resolve_runner_online_map(
        self,
        rows: list[dict[str, Any]],
    ) -> dict[str, bool | None]:
        """Resolve live connectivity for each unique runner in *rows*."""
        unique_ids: list[str] = []
        seen: set[str] = set()
        for r in rows:
            rid = r.get("runner_id")
            if isinstance(rid, str) and rid and rid not in seen:
                seen.add(rid)
                unique_ids.append(rid)
        results = await asyncio.gather(*(self.runner_online_or_none(rid) for rid in unique_ids))
        return dict(zip(unique_ids, results, strict=True))

    @staticmethod
    def _child_rows_to_entries(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, str | None]]:
        """Map child-session rows to ``sys_session_list`` entries."""
        entries: list[dict[str, str | None]] = []
        for row in rows:
            title = row.get("title")
            if not title or ":" not in title or is_session_closed(row.get("labels"), title):
                continue
            entries.append(
                {
                    "agent": row.get("tool"),
                    "title": row.get("session_name"),
                    "conversation_id": row.get("id"),
                }
            )
        return entries


__all__ = ["SessionQueryService"]
