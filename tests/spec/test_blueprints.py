"""Parser and validator coverage for deterministic blueprint specs."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnigent.spec.parser import parse
from omnigent.spec.validator import validate


def _write_config(root: Path, config: dict[str, object]) -> None:
    (root / "config.yaml").write_text(yaml.dump(config, sort_keys=False))


def test_parse_valid_blueprint_yaml(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "demo-blueprint",
            "params": {"workflow": True},
            "executor": {"type": "blueprint"},
            "blueprint": {
                "name": "Demo",
                "nodes": [
                    {"id": "collect", "kind": "task", "output": {"ideas": ["fast"]}},
                    {
                        "id": "done",
                        "kind": "output",
                        "depends_on": ["collect"],
                        "output": {"text": "{{ $.nodes.collect.output.ideas }}"},
                    },
                ],
                "outputs": {"text": "{{ $.nodes.done.output.text }}"},
            },
        },
    )

    spec = parse(tmp_path, expand_env=False)

    assert spec.executor.type == "blueprint"
    assert spec.blueprint is not None
    assert [node.id for node in spec.blueprint.nodes] == ["collect", "done"]
    assert validate(spec).valid


def test_blueprint_requires_blueprint_executor(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "wrong-executor",
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
            "blueprint": {"nodes": [{"id": "one", "kind": "task"}]},
        },
    )

    result = validate(parse(tmp_path, expand_env=False))

    assert not result.valid
    assert any(err.path == "executor.type" for err in result.errors)


def test_blueprint_rejects_unknown_dependency(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "bad-dep",
            "executor": {"type": "blueprint"},
            "blueprint": {
                "nodes": [
                    {"id": "draft", "kind": "task", "depends_on": ["missing"]},
                ],
            },
        },
    )

    result = validate(parse(tmp_path, expand_env=False))

    assert not result.valid
    assert any("unknown dependency" in err.message for err in result.errors)


def test_blueprint_rejects_unbounded_loop(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "bad-loop",
            "executor": {"type": "blueprint"},
            "blueprint": {
                "nodes": [
                    {
                        "id": "review",
                        "kind": "loop",
                        "loop": {
                            "body": [{"id": "draft", "kind": "task"}],
                        },
                    }
                ],
            },
        },
    )

    result = validate(parse(tmp_path, expand_env=False))

    assert not result.valid
    assert {err.path for err in result.errors} >= {
        "blueprint.nodes[0].loop.max_iterations",
        "blueprint.nodes[0].loop.until",
    }


def test_existing_prompt_workflow_agent_stays_valid(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "prompt-workflow",
            "params": {"workflow": True},
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
            "llm": {"model": "test", "connection": {"api_key": "test"}},
            "prompt": "Coordinate with prompt-driven sub-agents.",
        },
    )

    spec = parse(tmp_path, expand_env=False)

    assert spec.blueprint is None
    assert validate(spec).valid


def test_blueprint_playground_bundles_parse_and_validate() -> None:
    root = Path("deploy/bytedesk/agents")
    for bundle in [
        "demo-team-idea-collection",
        "demo-motto-drafting",
        "blueprint-playground-team-motto",
    ]:
        spec = parse(root / bundle, expand_env=False)
        result = validate(spec)

        assert spec.executor.type == "blueprint"
        assert spec.blueprint is not None
        assert result.valid, [f"{err.path}: {err.message}" for err in result.errors]
