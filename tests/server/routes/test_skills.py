"""Tests for the skill acquisition routes."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

from omnigent.db.utils import generate_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.bundles import bundle_location
from omnigent.spec.tar_utils import build_bundle_bytes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore

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


def _seed_template_agent(db_uri: str, tmp_path: Path, *, name: str = "demo") -> str:
    """Seed a template agent with a real bundle in the app's stores."""
    image = tmp_path / f"seed_image_{name}"
    (image / "skills" / "deep-search").mkdir(parents=True)
    (image / "config.yaml").write_text(_CONFIG.replace("name: demo", f"name: {name}"))
    (image / "AGENTS.md").write_text("You are demo.\n")
    (image / "skills" / "deep-search" / "SKILL.md").write_text(_SKILL)

    bundle_bytes = build_bundle_bytes(image)
    agent_id = generate_agent_id()
    loc = bundle_location(agent_id, bundle_bytes)
    LocalArtifactStore(str(tmp_path / "artifacts")).put(loc, bundle_bytes)
    SqlAlchemyAgentStore(db_uri).create(agent_id, name=name, bundle_location=loc)
    return agent_id


def _skill_generator_script() -> str:
    return (
        "from pathlib import Path\n"
        "p = Path('skills/image-tools')\n"
        "p.mkdir(parents=True)\n"
        "(p / 'assets').mkdir()\n"
        "(p / 'SKILL.md').write_text("
        "'---\\nname: image-tools\\ndescription: Work with images.\\n---\\nUse this skill.\\n'"
        ")\n"
        "(p / 'assets' / 'icon.bin').write_bytes(b'\\x00\\x01binary')\n"
    )


async def test_marketplaces_route_lists_registry_entries(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/skills/marketplaces")

    assert resp.status_code == 200, resp.text
    entries = {row["id"]: row for row in resp.json()["data"]}
    assert "github:ByteDeskAI-bytedesk-marketplace" in entries
    assert entries["github:ByteDeskAI-bytedesk-marketplace"]["label"] == "ByteDesk Catalog"
    assert "supercharge" in entries


async def test_sources_route_lists_framework_adapters(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/skills/sources")

    assert resp.status_code == 200, resp.text
    sources = {source["id"]: source for source in resp.json()["data"]}
    ids = set(sources)
    assert {"skills", "npm", "github", "configured", "freeform"}.issubset(ids)
    assert all("available" in source for source in sources.values())
    assert sources["freeform"]["supports_search"] is True


async def test_preview_and_apply_installs_full_skill_directory(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)

    preview = await client.post(
        "/v1/skills/previews",
        json={
            "target_agent_ids": [agent_id],
            "source": "freeform",
            "command": {
                "argv": [sys.executable, "-c", _skill_generator_script()],
                "timeout_seconds": 30,
            },
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["skills"][0]["name"] == "image-tools"
    assert any(f["binary"] for f in body["skills"][0]["files"])
    assert body["target_actions"][0]["action"] == "install"

    applied = await client.post(f"/v1/skills/previews/{body['id']}/apply", json={})
    assert applied.status_code == 200, applied.text
    result = applied.json()["data"][0]
    assert result["status"] == "applied"
    assert result["version"] == 2

    got = await client.get(f"/v1/agents/{agent_id}/image")
    assert "image-tools" in got.json()["skills"]

    store = SqlAlchemyAgentStore(db_uri)
    agent = store.get(agent_id)
    assert agent is not None
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "verify-cache")
    loaded = cache.load(agent.id, agent.bundle_location, expand_env=False)
    assert (loaded.workdir / "skills" / "image-tools" / "assets" / "icon.bin").read_bytes() == (
        b"\x00\x01binary"
    )


async def test_preview_marks_existing_skill_conflict_when_fail_on_existing(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)

    preview = await client.post(
        "/v1/skills/previews",
        json={
            "target_agent_ids": [agent_id],
            "install_mode": "fail_on_existing",
            "source": "freeform",
            "command": {
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path\n"
                    "p=Path('skills/deep-search'); p.mkdir(parents=True)\n"
                    "(p/'SKILL.md').write_text("
                    "'---\\nname: deep-search\\ndescription: Replacement.\\n---\\nBody.\\n')\n",
                ],
            },
        },
    )

    assert preview.status_code == 200, preview.text
    assert preview.json()["target_actions"][0]["action"] == "conflict"

    applied = await client.post(f"/v1/skills/previews/{preview.json()['id']}/apply", json={})
    assert applied.status_code == 200, applied.text
    assert applied.json()["data"][0]["status"] == "failed"


async def test_apply_rejects_stale_preview_without_clobber(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)

    preview = await client.post(
        "/v1/skills/previews",
        json={
            "target_agent_ids": [agent_id],
            "source": "freeform",
            "command": {
                "argv": [sys.executable, "-c", _skill_generator_script()],
            },
        },
    )
    assert preview.status_code == 200, preview.text

    bump = await client.put(
        f"/v1/agents/{agent_id}/image",
        json={"instructions": "changed first.\n"},
    )
    assert bump.status_code == 200, bump.text

    applied = await client.post(f"/v1/skills/previews/{preview.json()['id']}/apply", json={})
    assert applied.status_code == 200, applied.text
    result = applied.json()["data"][0]
    assert result["status"] == "failed"
    assert "stale" in result["error"]

    got = await client.get(f"/v1/agents/{agent_id}/image")
    assert got.json()["instructions"] == "changed first.\n"
    assert "image-tools" not in got.json()["skills"]


async def test_installed_route_aggregates_template_agent_skills(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    agent_id = _seed_template_agent(db_uri, tmp_path)

    resp = await client.get("/v1/skills/installed")

    assert resp.status_code == 200, resp.text
    skill = next(item for item in resp.json()["data"] if item["name"] == "deep-search")
    assert skill["agents"] == [{"id": agent_id, "name": "demo", "version": 1}]


async def test_installed_route_rejects_missing_agent_filter(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/skills/installed?agent_id=ag_missing")

    assert resp.status_code == 404, resp.text


async def test_concierge_session_opens_concierge_bound_session_idempotently(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    concierge_id = _seed_template_agent(db_uri, tmp_path, name="skills-concierge")

    resp = await client.post(
        "/v1/skills/concierge/sessions",
        json={
            "target_kind": "organization",
            "target_id": "omnigent",
            "target_label": "the whole organization",
            "target_agent_ids": [concierge_id],
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["agent_id"] == concierge_id
    assert body["agent_name"] == "skills-concierge"
    assert body["session_id"].startswith("conv_")
    assert "organization" in body["title"].lower()

    # Idempotent per (user, scope): re-opening the same scope returns the
    # same session rather than spawning a new one on each mount / scope change.
    again = await client.post(
        "/v1/skills/concierge/sessions",
        json={"target_kind": "organization", "target_id": "omnigent"},
    )
    assert again.status_code == 201, again.text
    assert again.json()["session_id"] == body["session_id"]


async def test_concierge_session_404_when_agent_not_registered(
    client: httpx.AsyncClient,
) -> None:
    # No skills-concierge agent seeded in this fresh DB.
    resp = await client.post(
        "/v1/skills/concierge/sessions",
        json={"target_kind": "organization", "target_id": "omnigent"},
    )

    assert resp.status_code == 404, resp.text
