"""Tests for the template-agent image routes (GET/PUT /v1/agents/{id}/image)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Agent
from omnigent.errors import OmnigentError
from omnigent.server.bundles import bundle_location
from omnigent.server.routes.agents_write import _require_template, _safe_join
from omnigent.spec.tar_utils import build_bundle_bytes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_CONFIG = (
    "spec_version: 1\n"
    "name: demo\n"
    "description: A demo.\n"
    "executor:\n"
    "  type: omnigent\n"
    "  config:\n"
    "    harness: claude-sdk\n"
)
_SKILL = "---\nname: deep-search\ndescription: Search stuff.\n---\nBody.\n"


def _config(harness: str = "claude-sdk", name: str = "demo") -> dict:
    """A valid omnigent agent config with the given harness/name."""
    return {
        "spec_version": 1,
        "name": name,
        "description": "A demo.",
        "executor": {"type": "omnigent", "config": {"harness": harness}},
    }


def _seed_agent(
    db_uri: str,
    tmp_path: Path,
    *,
    name: str = "demo",
    session_id: str | None = None,
) -> str:
    """Seed an agent with a real bundle in the app's stores."""
    image = tmp_path / f"seed_image_{name}"
    (image / "skills" / "deep-search").mkdir(parents=True)
    (image / "docs").mkdir(parents=True)
    (image / "assets").mkdir(parents=True)
    (image / "config.yaml").write_text(_CONFIG)
    (image / "AGENTS.md").write_text("You are demo.\n")
    (image / "skills" / "deep-search" / "SKILL.md").write_text(_SKILL)
    (image / "docs" / "guide.md").write_text("# Guide\n")
    (image / "assets" / "pixel.bin").write_bytes(b"\x89PNG\x00binary")

    bundle_bytes = build_bundle_bytes(image)
    agent_id = generate_agent_id()
    loc = bundle_location(agent_id, bundle_bytes)
    # Same backend locations the `app` fixture builds (tmp_path/"artifacts", db_uri).
    LocalArtifactStore(str(tmp_path / "artifacts")).put(loc, bundle_bytes)
    SqlAlchemyAgentStore(db_uri).create(
        agent_id, name=name, bundle_location=loc, session_id=session_id
    )
    return agent_id


def _seed_template_agent(db_uri: str, tmp_path: Path) -> str:
    """Seed a template agent with a real bundle in the app's stores."""
    return _seed_agent(db_uri, tmp_path)


# ── unit: guards ──────────────────────────────────────────────────────


def test_require_template_rejects_missing() -> None:
    with pytest.raises(OmnigentError):
        _require_template(None, "ag_missing")


def test_require_template_rejects_session_scoped() -> None:
    scoped = Agent(
        id="ag_x",
        created_at=0,
        name="demo",
        bundle_location="ag_x/h",
        session_id="conv_1",
    )
    with pytest.raises(OmnigentError):
        _require_template(scoped, "ag_x")


def test_safe_join_blocks_traversal(tmp_path: Path) -> None:
    for bad in ["../escape", "/abs", "a/../../b", "x\\y"]:
        with pytest.raises(OmnigentError):
            _safe_join(tmp_path, bad)
    assert _safe_join(tmp_path, "tools/mcp/jira.yaml").is_relative_to(tmp_path)


# ── integration: GET ──────────────────────────────────────────────────


async def test_get_image_returns_editable_surface(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.get(f"/v1/agents/{agent_id}/image")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "demo"
    assert body["config"]["executor"]["config"]["harness"] == "claude-sdk"
    assert body["instructions"] == "You are demo.\n"
    assert "deep-search" in body["skills"]


async def test_get_image_tree_lists_root_entries(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.get(f"/v1/agents/{agent_id}/image/tree")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "."
    assert resp.headers["etag"] == '"1"'
    entries = {entry["path"]: entry for entry in body["entries"]}
    assert ".omnigent-bundle-location" not in entries
    assert entries["config.yaml"]["type"] == "file"
    assert entries["AGENTS.md"]["type"] == "file"
    assert entries["skills"]["type"] == "directory"
    assert entries["docs"]["type"] == "directory"


async def test_get_image_tree_lists_nested_directory(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.get(f"/v1/agents/{agent_id}/image/tree", params={"path": "docs"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "docs"
    assert [entry["path"] for entry in body["entries"]] == ["docs/guide.md"]


async def test_get_image_file_reads_bounded_text_file(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.get(
        f"/v1/agents/{agent_id}/image/file",
        params={"path": "skills/deep-search/SKILL.md"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "skills/deep-search/SKILL.md"
    assert body["content"] == _SKILL
    assert body["size"] == len(_SKILL.encode())


async def test_get_image_file_rejects_traversal(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.get(
        f"/v1/agents/{agent_id}/image/file",
        params={"path": "../escape"},
    )

    assert resp.status_code == 400, resp.text


async def test_get_image_file_rejects_binary(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.get(
        f"/v1/agents/{agent_id}/image/file",
        params={"path": "assets/pixel.bin"},
    )

    assert resp.status_code == 400, resp.text


async def test_get_image_file_hides_cache_marker(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.get(
        f"/v1/agents/{agent_id}/image/file",
        params={"path": ".omnigent-bundle-location"},
    )

    assert resp.status_code == 404, resp.text


async def test_get_image_file_rejects_session_scoped_agent(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    conv = SqlAlchemyConversationStore(db_uri).create_conversation()
    agent_id = _seed_agent(db_uri, tmp_path, name="demo-scoped", session_id=conv.id)
    resp = await client.get(
        f"/v1/agents/{agent_id}/image/file",
        params={"path": "AGENTS.md"},
    )

    assert resp.status_code == 400, resp.text


# ── integration: PUT ──────────────────────────────────────────────────


async def test_put_image_changes_config_live(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)

    put = await client.put(
        f"/v1/agents/{agent_id}/image", json={"config": _config(harness="openai-agents")}
    )
    assert put.status_code == 200, put.text
    assert put.json()["version"] == 2  # bumped

    # Live: a subsequent read reflects the new harness (no server restart).
    got = await client.get(f"/v1/agents/{agent_id}/image")
    assert got.json()["config"]["executor"]["config"]["harness"] == "openai-agents"
    # Marked omnigent-SoT so the boot re-seed won't clobber the edit.
    assert SqlAlchemyAgentStore(db_uri).get_sot_tier(agent_id) == "migrated"


async def test_put_image_preserves_unedited_assets(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)

    await client.put(
        f"/v1/agents/{agent_id}/image", json={"config": _config(harness="openai-agents")}
    )

    got = await client.get(f"/v1/agents/{agent_id}/image")
    # A config-only edit must NOT strip the bundled skill.
    assert "deep-search" in got.json()["skills"]


async def test_put_image_rejects_name_change(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.put(
        f"/v1/agents/{agent_id}/image", json={"config": _config(name="renamed")}
    )
    assert resp.status_code >= 400


async def test_put_image_idempotent_second_put_is_noop(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    cfg = _config()
    # First PUT re-serializes config.yaml → content differs from the
    # hand-written seed, so it bumps once. The second identical PUT is a
    # content-address no-op and must NOT bump again.
    first = await client.put(f"/v1/agents/{agent_id}/image", json={"config": cfg})
    second = await client.put(f"/v1/agents/{agent_id}/image", json={"config": cfg})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["version"] == first.json()["version"]


# ── integration: If-Match / ETag optimistic concurrency (BDP-2412) ─────
# These exercise the real GET/PUT route through the ASGI app, doubling as the
# in-process smoke for the feature (GET ETag → conditional PUT → stale 412).


async def test_get_image_emits_version_etag_header(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.get(f"/v1/agents/{agent_id}/image")
    assert resp.status_code == 200, resp.text
    # ETag is the strong-validated agent version; the seed is version 1.
    assert resp.headers["etag"] == '"1"'
    assert resp.json()["version"] == 1


async def test_put_with_matching_if_match_succeeds(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.put(
        f"/v1/agents/{agent_id}/image",
        json={"config": _config(harness="openai-agents")},
        headers={"If-Match": '"1"'},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2


async def test_put_with_stale_if_match_returns_412(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)
    # advance to version 2 with no precondition
    bump = await client.put(
        f"/v1/agents/{agent_id}/image",
        json={"config": _config(harness="openai-agents")},
    )
    assert bump.json()["version"] == 2
    # If-Match "1" is now stale; a divergent edit must be REJECTED, not clobber
    stale = await client.put(
        f"/v1/agents/{agent_id}/image",
        json={"instructions": "stale writer.\n"},
        headers={"If-Match": '"1"'},
    )
    assert stale.status_code == 412, stale.text


async def test_put_without_if_match_is_unconditional(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    # back-compat: the current editor sends no If-Match and still works
    agent_id = _seed_template_agent(db_uri, tmp_path)
    resp = await client.put(
        f"/v1/agents/{agent_id}/image",
        json={"config": _config(harness="openai-agents")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2


async def test_concurrent_image_writes_no_clobber(
    client: httpx.AsyncClient, db_uri: str, tmp_path: Path
) -> None:
    # the named AgentImageUpdate clobber, end-to-end: two divergent edits from
    # base version 1 — exactly one wins (200), the other is rejected (412).
    agent_id = _seed_template_agent(db_uri, tmp_path)
    writer_a = await client.put(
        f"/v1/agents/{agent_id}/image",
        json={"instructions": "writer A.\n"},
        headers={"If-Match": '"1"'},
    )
    writer_b = await client.put(
        f"/v1/agents/{agent_id}/image",
        json={"instructions": "writer B.\n"},
        headers={"If-Match": '"1"'},
    )
    assert writer_a.status_code == 200, writer_a.text
    assert writer_b.status_code == 412, writer_b.text
    got = await client.get(f"/v1/agents/{agent_id}/image")
    assert got.json()["instructions"] == "writer A.\n"  # A won; B did not clobber
