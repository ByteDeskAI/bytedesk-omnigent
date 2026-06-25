"""Route tests for the agent-callable shared-memory route (BDP-2457).

Covers the team/topic happy paths (recall + append + compartments) against a
stubbed memory provider, the agent-scope fail-closed error, and the auth gate
(unauthenticated rejected in multi-user mode; open in single-user mode — the
posture the runner's MCP connection reaches today). All access goes through the
pluggable provider (BDP-2369), so a stub provider fully proves the route logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bytedesk_omnigent.routes.memory import create_memory_router
from omnigent.errors import OmnigentError


@dataclass
class _Hit:
    """Minimal stand-in for ``omnigent.stores.memory_store.Memory``."""

    id: str
    content: str
    effective_weight: float


class _StubProvider:
    """Records calls + returns canned data so the route logic is provable offline."""

    def __init__(self, *, hits: list[_Hit] | None = None, compartments=None) -> None:
        self._hits = hits or []
        self._compartments = compartments if compartments is not None else []
        self.recall_calls: list[dict] = []
        self.write_calls: list[dict] = []
        self.noted: list[list[_Hit]] = []
        self.list_calls: list[dict] = []

    def recall(self, *, scope, owner, name, query, k, kind="ambient"):
        self.recall_calls.append(
            {"scope": scope, "owner": owner, "name": name, "query": query, "k": k, "kind": kind}
        )
        return list(self._hits)

    def note_recalled(self, hits) -> None:
        self.noted.append(list(hits))

    def write(self, *, scope, owner, name, content, weight=1.0):
        self.write_calls.append(
            {"scope": scope, "owner": owner, "name": name, "content": content, "weight": weight}
        )
        return "mem_123"

    def list_compartments(self, *, scope=None, owner=None):
        self.list_calls.append({"scope": scope, "owner": owner})
        return [c for c in self._compartments if c["scope"] == scope]


class _NoIdentityAuth:
    """A multi-user auth provider that never resolves an identity → forces 401."""

    def get_user_id(self, request: object) -> None:
        return None


def _app(auth_provider: object | None = None) -> FastAPI:
    app = FastAPI()
    # Mirror the main app's OmnigentError → http_status mapping so require_user's
    # UNAUTHORIZED surfaces as a real 401 (same shape as the governance test).
    app.add_exception_handler(
        OmnigentError,
        lambda request, exc: JSONResponse(
            status_code=exc.http_status, content={"error": exc.code}
        ),
    )
    app.include_router(create_memory_router(auth_provider=auth_provider), prefix="/v1")
    return app


def _client(auth_provider: object | None = None) -> TestClient:
    return TestClient(_app(auth_provider), raise_server_exceptions=False)


# ── team / topic happy paths ──────────────────────────────────────────────────


def test_recall_team_scope_stamps_team_owner(monkeypatch) -> None:
    provider = _StubProvider(hits=[_Hit("m1", "we ship fridays", 1.2345)])
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)

    resp = _client().post(
        "/v1/memory/recall",
        json={"query": "release cadence", "scope": "team", "name": "org-context", "limit": 5},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "results": [{"content": "we ship fridays", "weight": 1.2345, "memory_id": "m1"}]
    }
    # owner is server-stamped to the constant "team" (never from the body).
    assert provider.recall_calls == [
        {
            "scope": "team",
            "owner": "team",
            "name": "org-context",
            "query": "release cadence",
            "k": 5,
            "kind": "all",  # search spans ambient + addressable by default (BDP-2459)
        }
    ]
    # out-of-band reinforcement fired.
    assert provider.noted and provider.noted[0][0].id == "m1"


def test_recall_topic_scope_stamps_shared_owner(monkeypatch) -> None:
    provider = _StubProvider(hits=[])
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)

    resp = _client().post(
        "/v1/memory/recall",
        json={"query": "blockers", "scope": "topic", "name": "dept:engineering"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"results": [], "message": "No matching memories."}
    assert provider.recall_calls[0]["owner"] == "shared"
    assert provider.recall_calls[0]["name"] == "dept:engineering"


def test_append_team_scope_writes_team_owner(monkeypatch) -> None:
    provider = _StubProvider()
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)

    resp = _client().post(
        "/v1/memory/append",
        json={"content": "Q3 focus is first customer", "scope": "team", "name": "org-context"},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "memory_id": "mem_123",
        "scope": "team",
        "compartment": "org-context",
    }
    call = provider.write_calls[0]
    # owner is server-stamped to the constant "team" (never from the body).
    assert call["owner"] == "team"
    assert call["scope"] == "team"
    assert call["name"] == "org-context"
    assert call["content"] == "Q3 focus is first customer"
    assert call["weight"] == 1.0


def test_append_topic_scope_writes_shared_owner(monkeypatch) -> None:
    provider = _StubProvider()
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)

    resp = _client().post(
        "/v1/memory/append",
        json={
            "content": "spec drafted",
            "scope": "topic",
            "name": "initiative:bdp-2457",
            "weight": 2.0,
        },
    )

    assert resp.status_code == 200
    assert provider.write_calls[0]["owner"] == "shared"
    assert provider.write_calls[0]["weight"] == 2.0


def test_compartments_lists_shared_and_always_surfaces_org_blackboard(monkeypatch) -> None:
    provider = _StubProvider(
        compartments=[{"scope": "topic", "name": "initiative:bdp-2457"}]
    )
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)

    resp = _client().post("/v1/memory/compartments", json={})

    assert resp.status_code == 200
    comps = resp.json()["compartments"]
    # The standing team/org-context blackboard is always present
    # (ensure_org_compartments), even though the stub never listed it under team.
    assert {"scope": "team", "name": "org-context"} in comps
    assert {"scope": "topic", "name": "initiative:bdp-2457"} in comps
    # only team + topic scopes were queried (no agent scope).
    assert {c["scope"] for c in provider.list_calls} == {"team", "topic"}


# ── agent scope fails closed ──────────────────────────────────────────────────


def test_agent_scope_recall_fails_closed(monkeypatch) -> None:
    provider = _StubProvider()
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)

    resp = _client().post("/v1/memory/recall", json={"query": "x", "scope": "agent"})

    assert resp.status_code == 400
    assert "verified per-agent identity" in resp.json()["error"]
    # never touched the provider.
    assert provider.recall_calls == []


def test_agent_scope_append_fails_closed(monkeypatch) -> None:
    provider = _StubProvider()
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)

    resp = _client().post("/v1/memory/append", json={"content": "secret", "scope": "agent"})

    assert resp.status_code == 400
    assert "verified per-agent identity" in resp.json()["error"]
    assert provider.write_calls == []


def test_unknown_scope_rejected(monkeypatch) -> None:
    provider = _StubProvider()
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)

    resp = _client().post("/v1/memory/recall", json={"query": "x", "scope": "tenant"})

    assert resp.status_code == 400
    assert "invalid memory scope" in resp.json()["error"]


# ── auth gate (the runner/service-call reachability contract) ────────────────


def test_recall_requires_auth_in_multi_user_mode() -> None:
    # No provider patch needed: require_user rejects before the handler runs.
    assert _client(_NoIdentityAuth()).post(
        "/v1/memory/recall", json={"query": "x", "scope": "team"}
    ).status_code == 401


def test_append_requires_auth_in_multi_user_mode() -> None:
    assert _client(_NoIdentityAuth()).post(
        "/v1/memory/append", json={"content": "x", "scope": "team"}
    ).status_code == 401


def test_compartments_requires_auth_in_multi_user_mode() -> None:
    assert _client(_NoIdentityAuth()).post(
        "/v1/memory/compartments", json={}
    ).status_code == 401


def test_single_user_mode_accepts_the_unauthenticated_runner_call(monkeypatch) -> None:
    # auth_provider=None ⇒ single-user mode (the live OMNIGENT_AUTH_ENABLED=0
    # header/single-user deployment): the runner's MCP connection reaches the
    # handler with no auth header. Mirrors how the route is wired in prod today.
    provider = _StubProvider(hits=[])
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)

    resp = _client(None).post("/v1/memory/recall", json={"query": "x", "scope": "team"})

    assert resp.status_code == 200
    assert provider.recall_calls[0]["owner"] == "team"


# ── addressable (keyed) memory against a REAL in-memory store ─────────────────
#
# get/put/unset touch the durable store (keyed exact-key SQL), so they are proved
# end-to-end against a fresh SQLite store rather than a stub.

import uuid  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture()
def real_provider(monkeypatch):
    """A live ComposedAgentMemoryProvider over a private shared-cache SQLite DB."""
    from omnigent.stores.memory_store.provider import ComposedAgentMemoryProvider

    # Named shared-cache in-memory URI so every connection sees the same DB.
    uri = f"sqlite:///file:mem_{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"
    provider = ComposedAgentMemoryProvider.from_location(uri)
    monkeypatch.setattr("omnigent.runtime.get_memory_provider", lambda: provider)
    return provider


def test_put_then_get_exact_address(real_provider) -> None:
    client = _client()
    put = client.post("/v1/memory/put", json={"address": "org:charter", "content": "be the best"})
    assert put.status_code == 200
    assert put.json()["overwrote"] == 0

    got = client.post("/v1/memory/get", json={"address": "org:charter"})
    assert got.status_code == 200
    body = got.json()
    assert body["found"] is True
    assert body["content"] == "be the best"
    assert body["address"] == "org:charter"


def test_get_missing_address_returns_not_found(real_provider) -> None:
    got = _client().post("/v1/memory/get", json={"address": "org:never-set"})
    assert got.status_code == 200
    assert got.json() == {"address": "org:never-set", "found": False}


def test_put_overwrites_in_place(real_provider) -> None:
    client = _client()
    client.post("/v1/memory/put", json={"address": "org:focus", "content": "v1"})
    second = client.post("/v1/memory/put", json={"address": "org:focus", "content": "v2"})
    assert second.json()["overwrote"] == 1

    # exactly the new value, deterministically.
    assert client.post("/v1/memory/get", json={"address": "org:focus"}).json()["content"] == "v2"

    # and the slot did not duplicate — exactly ONE live row carries the key (on the
    # first-class ``key`` column; the partial-unique live-slot index enforces it).
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlMemory

    with Session(real_provider.store.engine) as session:
        rows = (
            session.execute(
                select(SqlMemory).where(
                    SqlMemory.archived.is_(False), SqlMemory.key == "focus"
                )
            )
            .scalars()
            .all()
        )
    assert [r.content for r in rows] == ["v2"]


def test_dept_address_targets_topic_compartment(real_provider) -> None:
    client = _client()
    client.post("/v1/memory/put", json={"address": "dept:engineering:oncall", "content": "alice"})
    got = client.post("/v1/memory/get", json={"address": "dept:engineering:oncall"})
    assert got.json()["content"] == "alice"
    # it landed in topic/dept:engineering, not org-context.
    comps = {(c["scope"], c["name"]) for c in real_provider.list_compartments(scope="topic")}
    assert ("topic", "dept:engineering") in comps


def test_keys_are_isolated_within_a_compartment(real_provider) -> None:
    client = _client()
    client.post("/v1/memory/put", json={"address": "org:charter", "content": "C"})
    client.post("/v1/memory/put", json={"address": "org:goals", "content": "G"})
    assert client.post("/v1/memory/get", json={"address": "org:charter"}).json()["content"] == "C"
    assert client.post("/v1/memory/get", json={"address": "org:goals"}).json()["content"] == "G"


def test_unset_clears_the_slot(real_provider) -> None:
    client = _client()
    client.post("/v1/memory/put", json={"address": "org:temp", "content": "x"})
    cleared = client.post("/v1/memory/unset", json={"address": "org:temp"})
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] == 1
    assert client.post("/v1/memory/get", json={"address": "org:temp"}).json()["found"] is False


# ── BDP-2459: search spans ambient + addressable; list; all-agents mount ──────


def _search(client, kind: str) -> set[str]:
    r = client.post("/v1/memory/recall", json={"query": "pricing", "scope": "team", "kind": kind})
    return {x["content"] for x in r.json().get("results", [])}


def test_search_kind_spans_ambient_and_addressable(real_provider) -> None:
    client = _client()
    client.post(
        "/v1/memory/append", json={"content": "pricing rules are flexible", "scope": "team"}
    )
    client.post(
        "/v1/memory/put", json={"address": "org:pricing-sheet", "content": "pricing sheet v2"}
    )

    both = _search(client, "all")
    assert "pricing rules are flexible" in both  # ambient
    assert "pricing sheet v2" in both  # addressable
    assert _search(client, "ambient") == {"pricing rules are flexible"}
    assert _search(client, "addressable") == {"pricing sheet v2"}


def test_list_browses_keyed_slots_not_ambient(real_provider) -> None:
    client = _client()
    client.post("/v1/memory/put", json={"address": "org:charter", "content": "C"})
    client.post("/v1/memory/put", json={"address": "org:goals", "content": "G"})
    client.post("/v1/memory/append", json={"content": "ambient note", "scope": "team"})

    r = client.post("/v1/memory/list", json={"prefix": "org"})
    assert r.status_code == 200
    slots = r.json()["slots"]
    assert {s["key"] for s in slots} == {"charter", "goals"}  # keyed only, not ambient
    assert {s["content"] for s in slots} == {"C", "G"}


def test_list_rejects_agent_prefix(real_provider) -> None:
    r = _client().post("/v1/memory/list", json={"prefix": "agent:maya"})
    assert r.status_code == 400
    assert "verified per-agent identity" in r.json()["error"]


def test_extension_contributes_memory_default_mount_for_all_agents() -> None:
    from bytedesk_omnigent.extension import BytedeskExtension

    servers = BytedeskExtension().default_mcp_servers()
    assert len(servers) == 1
    m = servers[0]
    assert m.name == "memory"
    assert m.transport == "stdio"
    assert m.command == "python"
    assert m.args == ["-m", "bytedesk_omnigent.memory_mcp"]
    # PYTHONPATH=/build so the spawned stdio subprocess can import bytedesk_omnigent
    # (the SDK's minimal stdio env omits it) — the post-BDP-2457 mount-load fix.
    assert m.env.get("PYTHONPATH") == "/build"
    assert set(m.tool_allowlist) == {"search", "get", "put", "append", "list", "unset"}


def test_unset_missing_slot_is_not_found(real_provider) -> None:
    resp = _client().post("/v1/memory/unset", json={"address": "org:ghost"})
    assert resp.status_code == 200
    assert resp.json() == {"address": "org:ghost", "found": False}


def test_put_threads_provenance(real_provider) -> None:
    client = _client()
    client.post(
        "/v1/memory/put",
        json={
            "address": "org:charter",
            "content": "with source",
            "confidence": 0.7,
            "source_conversation_id": "conv_9",
        },
    )
    got = client.post("/v1/memory/get", json={"address": "org:charter"}).json()
    assert got["confidence"] == 0.7
    assert got["source_conversation_id"] == "conv_9"


# ── addressable endpoints validate + fail closed ─────────────────────────────


@pytest.mark.parametrize(
    "address,fragment",
    [
        ("agent:maya:style", "verified per-agent identity"),
        ("weird:x", "unknown address class"),
        ("org:", "org address must be"),
        ("dept:eng", "dept address must be"),
        ("", "address is required"),
    ],
)
def test_get_rejects_bad_addresses(real_provider, address, fragment) -> None:
    resp = _client().post("/v1/memory/get", json={"address": address})
    assert resp.status_code == 400
    assert fragment in resp.json()["error"]


def test_put_agent_address_fails_closed(real_provider) -> None:
    resp = _client().post("/v1/memory/put", json={"address": "agent:maya:x", "content": "secret"})
    assert resp.status_code == 400
    assert "verified per-agent identity" in resp.json()["error"]


def test_addressable_endpoints_require_auth_in_multi_user_mode() -> None:
    client = _client(_NoIdentityAuth())
    assert client.post("/v1/memory/get", json={"address": "org:c"}).status_code == 401
    put = client.post("/v1/memory/put", json={"address": "org:c", "content": "x"})
    assert put.status_code == 401
    assert client.post("/v1/memory/unset", json={"address": "org:c"}).status_code == 401
