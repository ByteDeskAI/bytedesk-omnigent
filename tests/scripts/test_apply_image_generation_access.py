from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "bytedesk"
    / "apply_image_generation_access.py"
)
spec = importlib.util.spec_from_file_location("apply_image_generation_access", SCRIPT_PATH)
assert spec is not None
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_target_agents_selects_design_web_dev_and_marketing_personas() -> None:
    agents = [
        {
            "id": "ag_brand",
            "name": "brand-and-creative-director",
            "display_name": "Mara Ellis",
            "params": {
                "title": "Brand & Creative Lead",
                "department": "Marketing",
            },
        },
        {
            "id": "ag_design",
            "name": "web-design-director",
            "display_name": "Avery Brooks",
            "params": {
                "title": "Product/Web Design Lead",
                "department": "Product",
            },
        },
        {
            "id": "ag_webdev",
            "name": "web-development-lead",
            "display_name": "Nolan Price",
            "params": {
                "title": "Web Development Lead",
                "department": "Engineering",
            },
        },
        {
            "id": "ag_backend",
            "name": "backend-development-lead",
            "display_name": "Priya Nair",
            "params": {
                "title": "Backend Development Lead",
                "department": "Engineering",
            },
        },
        {
            "id": "ag_workflow",
            "name": "seo-geo-growth-program",
            "display_name": "SEO/GEO Growth Program",
            "workflow": True,
            "params": {"department": "Marketing"},
        },
        {
            "id": "ag_program",
            "name": "demo-video-and-launch-asset-factory",
            "display_name": None,
            "params": {"department": "Marketing"},
        },
    ]

    selection = module.select_target_agents(agents)

    assert [agent["id"] for agent in selection.selected] == [
        "ag_brand",
        "ag_design",
        "ag_webdev",
    ]
    assert selection.skipped_workflows == ["ag_workflow"]
    assert selection.skipped_non_personas == ["ag_program"]


def test_target_agents_allows_explicit_workflow_override() -> None:
    agents = [
        {
            "id": "ag_workflow",
            "name": "seo-geo-growth-program",
            "display_name": "SEO/GEO Growth Program",
            "workflow": True,
            "params": {"department": "Marketing"},
        },
    ]

    selection = module.select_target_agents(agents, explicit_ids={"ag_workflow"})

    assert [agent["id"] for agent in selection.selected] == ["ag_workflow"]
    assert selection.skipped_workflows == []
    assert selection.skipped_non_personas == []


def test_target_agents_allows_explicit_non_persona_override() -> None:
    agents = [
        {
            "id": "ag_program",
            "name": "demo-video-and-launch-asset-factory",
            "display_name": None,
            "params": {"department": "Marketing"},
        },
    ]

    selection = module.select_target_agents(agents, explicit_ids={"ag_program"})

    assert [agent["id"] for agent in selection.selected] == ["ag_program"]
    assert selection.skipped_workflows == []
    assert selection.skipped_non_personas == []


def test_ensure_image_generation_config_merges_builtin_and_prompt() -> None:
    updated, changed = module.ensure_image_generation_config(
        {
            "name": "web-design-director",
            "prompt": "You are Avery.",
            "executor": {
                "type": "omnigent",
                "model": "gpt-5.4-mini",
                "config": {
                    "harness": "claude-sdk",
                    "keep": True,
                },
            },
            "tools": {
                "builtins": ["web_search"],
                "agentic-inbox": {
                    "type": "mcp",
                    "url": "${AGENTIC_INBOX_MCP_URL}",
                },
            },
        }
    )

    assert changed is True
    assert updated["executor"] == {
        "type": "omnigent",
        "model": "gpt-5.5",
        "config": {
            "harness": "codex",
            "keep": True,
        },
    }
    assert updated["tools"]["builtins"] == [
        "web_search",
        {"name": "bytedesk_generate_image"},
    ]
    assert "agentic-inbox" in updated["tools"]
    assert updated["skills"] == ["imagegen"]
    assert "CODEX IMAGE GENERATION" in updated["prompt"]
    assert "codex harness" in updated["prompt"]
    assert "gpt-5.5" in updated["prompt"]
    assert "bytedesk_generate_image" in updated["prompt"]
    assert "file_id" in updated["prompt"]


def test_ensure_image_generation_config_creates_codex_harness_executor() -> None:
    updated, changed = module.ensure_image_generation_config({"prompt": "You are Avery."})

    assert changed is True
    assert updated["executor"] == {
        "type": "omnigent",
        "model": "gpt-5.5",
        "config": {"harness": "codex"},
    }


def test_ensure_image_generation_config_accepts_existing_dict_builtin() -> None:
    updated, changed = module.ensure_image_generation_config(
        {
            "prompt": "You are Mara.",
            "tools": {
                "builtins": [
                    {"name": "bytedesk_generate_image"},
                ],
            },
        }
    )

    assert changed is True
    assert updated["tools"]["builtins"] == [
        {"name": "bytedesk_generate_image"},
    ]
    assert updated["skills"] == ["imagegen"]
    assert "CODEX IMAGE GENERATION" in updated["prompt"]


def test_ensure_image_generation_config_preserves_existing_skill_filter() -> None:
    updated, changed = module.ensure_image_generation_config(
        {
            "prompt": "You are Sofia.",
            "skills": ["research", "imagegen"],
        }
    )

    assert changed is True
    assert updated["skills"] == ["research", "imagegen"]


def test_ensure_image_generation_config_is_idempotent() -> None:
    updated, changed = module.ensure_image_generation_config({"prompt": "You are Sofia."})
    assert changed is True

    updated_again, changed_again = module.ensure_image_generation_config(updated)

    assert changed_again is False
    assert updated_again == updated


def test_ensure_image_generation_config_replaces_existing_note() -> None:
    updated, changed = module.ensure_image_generation_config(
        {
            "prompt": (
                "You are Nolan.\n\n"
                "CODEX IMAGE GENERATION\n"
                "- Old note.\n"
            )
        }
    )

    assert changed is True
    assert updated["prompt"].count("CODEX IMAGE GENERATION") == 1
    assert "Old note" not in updated["prompt"]
    assert "bytedesk_generate_image" in updated["prompt"]


def test_build_image_update_body_installs_bundled_skill() -> None:
    body, changed = module.build_image_update_body(
        {
            "prompt": "You are Avery.",
            "tools": {"builtins": []},
            "skills": "none",
        },
        existing_skills=[],
    )

    assert changed is True
    assert body["config"]["skills"] == ["imagegen"]
    assert "skills/imagegen/SKILL.md" in body["files"]
    assert "Codex-native path" in body["files"]["skills/imagegen/SKILL.md"]


def test_build_image_update_body_is_idempotent_when_skill_is_present() -> None:
    config, changed = module.ensure_image_generation_config(
        {
            "prompt": "You are Avery.",
            "tools": {"builtins": []},
            "skills": "none",
        }
    )
    assert changed is True

    body, changed_again = module.build_image_update_body(
        config,
        existing_skills=["imagegen"],
    )

    assert changed_again is False
    assert body == {"config": config}
