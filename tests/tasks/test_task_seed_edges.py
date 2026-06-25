"""Edge tests for workflow task seed parsing and upsert branches."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from bytedesk_omnigent.tasks.seed import (
    _extract_specialists,
    _upsert_seed,
    build_task_seed,
    parse_workflow_bundle,
    seed_workflow_tasks,
    workflow_bundle_dirs,
)
from bytedesk_omnigent.tasks.store import SqlAlchemyTaskStore
from omnigent.db.utils import make_managed_session_maker


def _write_bundle(base: Path, name: str, spec: dict) -> Path:
    bundle = base / name
    bundle.mkdir(parents=True)
    (bundle / "config.yaml").write_text(yaml.safe_dump(spec))
    return bundle


def test_workflow_bundle_dirs_skips_empty_and_non_dirs(tmp_path: Path) -> None:
    good = _write_bundle(
        tmp_path,
        "wf",
        {"name": "wf", "params": {"workflow": True, "orchestrator": "a", "department": "Ops"}},
    )
    env = f"::{os.pathsep}{good}{os.pathsep}{tmp_path / 'missing'}"
    assert workflow_bundle_dirs(env) == [good]


def test_parse_workflow_bundle_logs_and_skips_unreadable_config(
    tmp_path: Path, monkeypatch
) -> None:
    bundle = tmp_path / "unreadable"
    bundle.mkdir()
    config = bundle / "config.yaml"
    config.write_text("name: ok")

    def _boom(_self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(type(config), "read_text", _boom)
    assert parse_workflow_bundle(bundle) is None


def test_parse_workflow_bundle_handles_malformed_specs(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "bad"
    bad_yaml.mkdir()
    (bad_yaml / "config.yaml").write_text(":::not yaml")
    assert parse_workflow_bundle(bad_yaml) is None

    not_dict = _write_bundle(tmp_path, "list", ["not", "a", "dict"])
    assert parse_workflow_bundle(not_dict) is None

    missing_fields = _write_bundle(
        tmp_path,
        "incomplete",
        {"name": "incomplete", "params": {"workflow": True, "orchestrator": "lead"}},
    )
    assert parse_workflow_bundle(missing_fields) is None


def test_extract_specialists_returns_empty_on_bad_guardrails() -> None:
    assert _extract_specialists({}) == ()
    assert _extract_specialists({"guardrails": {"policies": {}}}) == ()
    assert (
        _extract_specialists(
            {
                "guardrails": {
                    "policies": {
                        "allowed_subagents": {
                            "function": {"arguments": {"allowed_agents": "not-a-list"}}
                        }
                    }
                }
            }
        )
        == ()
    )


def test_upsert_seed_reports_unchanged_when_row_matches(tmp_path: Path) -> None:
    store = SqlAlchemyTaskStore(f"sqlite:///{tmp_path / 'tasks.db'}")
    bundle = parse_workflow_bundle(
        _write_bundle(
            tmp_path,
            "demo",
            {
                "name": "demo",
                "params": {"workflow": True, "orchestrator": "lead", "department": "Ops"},
            },
        )
    )
    assert bundle is not None
    seed = build_task_seed(bundle)
    session_maker = make_managed_session_maker(store.engine, immediate=True)
    assert _upsert_seed(session_maker, seed, now=1000) == "inserted"
    assert _upsert_seed(session_maker, seed, now=2000) == "unchanged"


def test_seed_workflow_tasks_uses_default_store_and_counts_updates(
    tmp_path: Path, monkeypatch
) -> None:
    store = SqlAlchemyTaskStore(f"sqlite:///{tmp_path / 'tasks.db'}")
    bundle_dir = _write_bundle(
        tmp_path,
        "wf-one",
        {
            "name": "wf-one",
            "description": "v1",
            "params": {"workflow": True, "orchestrator": "lead", "department": "Ops"},
        },
    )
    monkeypatch.setattr("bytedesk_omnigent.tasks.get_task_store", lambda: store)

    assert seed_workflow_tasks(bundle_dirs=[bundle_dir], now=1000) == 1

    (bundle_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "wf-one",
                "description": "v2",
                "params": {"workflow": True, "orchestrator": "lead", "department": "Ops"},
            }
        )
    )
    assert seed_workflow_tasks(bundle_dirs=[bundle_dir], now=2000) == 1
    row = store.list_tasks()[0]
    assert row.title == "v2"


def test_seed_workflow_tasks_skips_unparseable_bundle_dirs(tmp_path: Path, monkeypatch) -> None:
    store = SqlAlchemyTaskStore(f"sqlite:///{tmp_path / 'tasks.db'}")
    bad = _write_bundle(tmp_path, "bad", {"name": "bad", "params": {"workflow": False}})
    good = _write_bundle(
        tmp_path,
        "good",
        {
            "name": "good",
            "params": {"workflow": True, "orchestrator": "lead", "department": "Ops"},
        },
    )
    monkeypatch.setattr("bytedesk_omnigent.tasks.get_task_store", lambda: store)
    assert seed_workflow_tasks(bundle_dirs=[bad, good], now=1000) == 1
