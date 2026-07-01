"""Tests for the workflow-orchestrator Task seeder (BDP-2337, ADR-0142).

ADDITIVE / DUAL-PRESENCE: the seeder derives one durable Task per ``params.workflow: true``
bundle from the same ``OMNIGENT_BUILTIN_AGENT_DIRS`` source the roster uses. These tests
pin the derived fields, the all-50 workflow-marked bundle count, and idempotency.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from bytedesk_omnigent.tasks.seed import (
    build_task_seed,
    derive_capabilities,
    parse_workflow_bundle,
    seed_workflow_tasks,
)
from bytedesk_omnigent.tasks.store import SqlAlchemyTaskStore

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENTS_DIR = _REPO_ROOT / "deploy" / "bytedesk" / "agents"


def _store(tmp_path) -> SqlAlchemyTaskStore:
    return SqlAlchemyTaskStore(f"sqlite:///{tmp_path / 'tasks.db'}")


def _real_workflow_dirs() -> list[Path]:
    dirs = []
    for config in sorted(_AGENTS_DIR.glob("*/config.yaml")):
        spec = yaml.safe_load(config.read_text()) or {}
        if (spec.get("params") or {}).get("workflow"):
            dirs.append(config.parent)
    return dirs


def _write_bundle(base: Path, name: str, spec: dict) -> Path:
    bundle = base / name
    bundle.mkdir(parents=True)
    (bundle / "config.yaml").write_text(yaml.safe_dump(spec))
    return bundle


def test_derive_fields_owner_capability_and_payload() -> None:
    spec = {
        "name": "demo-pipeline",
        "description": "Run the demo pipeline.",
        "prompt": "You are the demo orchestrator.\nDelegate to the team.\n",
        "params": {
            "workflow": True,
            "orchestrator": "sales-enablement-lead",
            "department": "Revenue",
            "cadence": "on-demand",
        },
        "guardrails": {
            "policies": {
                "allowed_subagents": {
                    "function": {
                        "arguments": {
                            "allowed_agents": ["marketing-director", "product-ops-director"]
                        }
                    }
                }
            }
        },
    }
    bundle = parse_workflow_bundle(_write_temp(spec))
    assert bundle is not None
    seed = build_task_seed(bundle)

    # owner == assignee == orchestrator; capability == department slug.
    assert seed.owner_agent_id == "sales-enablement-lead"
    assert seed.assignee_agent_id == "sales-enablement-lead"
    assert seed.required_capability == "revenue"
    assert seed.id == "task_wf_demo-pipeline"
    assert seed.title == "Run the demo pipeline."

    # Capabilities fold department + delegated specialists, de-duped, dept first.
    assert derive_capabilities(bundle) == [
        "revenue",
        "marketing-director",
        "product-ops-director",
    ]
    assert seed.payload["capabilities"][0] == "revenue"
    assert seed.payload["orchestrator"] == "sales-enablement-lead"
    assert seed.payload["specialists"] == ["marketing-director", "product-ops-director"]
    assert seed.payload["intent"].startswith("You are the demo orchestrator.")


def test_non_workflow_bundle_is_skipped() -> None:
    spec = {"name": "plain-agent", "params": {"workflow": False}}
    assert parse_workflow_bundle(_write_temp(spec)) is None


def test_seeds_all_seedable_real_workflow_bundles_idempotent(tmp_path) -> None:
    workflow_dirs = _real_workflow_dirs()
    assert len(workflow_dirs) == 50, "expected exactly 50 params.workflow:true bundles"

    store = _store(tmp_path)
    env_value = os.pathsep.join(str(d) for d in workflow_dirs)

    first = seed_workflow_tasks(store=store, env_value=env_value, now=1000)
    assert first == 46
    rows = store.list_tasks()
    assert len(rows) == 46

    # Every row owns/assigns to its bundle's orchestrator and gates on a dept slug.
    owners = {r.id: r.owner_agent_id for r in rows}
    assert owners["task_wf_goal-triage-router"] == "chief-of-staff"
    assert owners["task_wf_weekly-architecture-audit"] == "platform-architect"
    assert owners["task_wf_website-design-to-zip-factory"] == "product-ops-director"
    for r in rows:
        assert r.owner_agent_id == r.assignee_agent_id  # owner==assignee for templates
        assert r.required_capability  # a department-slug capability is always set
        assert r.source == "workflow-bundle"

    # Second run stays at 46 seedable rows — stable ids make re-seeding an upsert.
    second = seed_workflow_tasks(store=store, env_value=env_value, now=2000)
    assert second == 46
    assert len(store.list_tasks()) == 46


def test_reseed_preserves_runtime_status(tmp_path) -> None:
    workflow_dirs = _real_workflow_dirs()[:1]
    store = _store(tmp_path)
    env_value = os.pathsep.join(str(d) for d in workflow_dirs)

    seed_workflow_tasks(store=store, env_value=env_value, now=1000)
    task_id = store.list_tasks()[0].id
    store.advance_task(task_id=task_id, status="in_progress", now=1500)

    # A re-seed refreshes derived fields but must NOT reset an advanced task.
    seed_workflow_tasks(store=store, env_value=env_value, now=2000)
    refreshed = store.list_tasks(status="in_progress")
    assert [t.id for t in refreshed] == [task_id]


# ── helpers ─────────────────────────────────────────────────────────────────
import tempfile  # noqa: E402


def _write_temp(spec: dict) -> Path:
    tmp = Path(tempfile.mkdtemp())
    name = str(spec.get("name") or "bundle")
    return _write_bundle(tmp, name, spec)
