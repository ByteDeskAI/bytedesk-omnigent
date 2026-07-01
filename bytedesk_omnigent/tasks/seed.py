"""Seed the workflow orchestrators as first-class Tasks (BDP-2337, ADR-0142).

The ``params.workflow: true`` agent bundles are orchestrators: each decomposes
a recurring piece of org work and delegates it to a specialist roster. BDP-2337
represents each one as a durable :class:`~bytedesk_omnigent.tasks.store.Task`
so the automation suite is visible + assignable on the task substrate.

**ADDITIVE / DUAL-PRESENCE (hard rule).** The workflow agents stay in the roster
byte-for-byte — this seeder only *adds* derived Task rows. It reads the SAME bundle
dirs the server seeds the roster from (``OMNIGENT_BUILTIN_AGENT_DIRS``), so the Task
substrate and the roster share one source of truth instead of a drifting copy.

**Idempotent.** Each bundle maps to a stable id ``task_wf_<bundle-name>`` so a re-seed
is an UPSERT (refresh the derived fields), never a duplicate. The runtime ``status``
is preserved on update so re-seeding never resets an in-flight orchestrator task; only
the template-derived fields (title / owner / assignee / capability / payload) refresh.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bytedesk_omnigent.db_models import SqlTask
from omnigent.db.utils import make_managed_session_maker, now_epoch

logger = logging.getLogger(__name__)

#: ``source`` stamp marking a row as a workflow-orchestrator seed (queryable + scopes
#: re-seeds). Distinct from runtime-created tasks.
SEED_SOURCE = "workflow-bundle"

#: Env var the server uses to list built-in agent bundle dirs (see
#: ``omnigent.server.app._EXTRA_BUILTIN_AGENTS_ENV``). The seeder reuses it so the
#: Task rows derive from the exact roster source — no separate catalog to drift.
BUNDLE_DIRS_ENV = "OMNIGENT_BUILTIN_AGENT_DIRS"


def _slug(value: str) -> str:
    """Lowercase + hyphenate a free-form label into a capability/dept slug.

    ``"Operations"`` → ``"operations"``; an already-slugged ``"people-ops"`` is left
    as-is. Collapses any run of non-alphanumerics to a single hyphen.
    """
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


@dataclass(frozen=True)
class WorkflowBundle:
    """A parsed ``params.workflow: true`` orchestrator bundle (the seed source)."""

    name: str
    orchestrator: str
    department: str
    description: str
    cadence: str | None
    specialists: tuple[str, ...] = field(default_factory=tuple)
    intent: str = ""


def workflow_bundle_dirs(env_value: str | None = None) -> list[Path]:
    """Resolve the bundle dirs from ``OMNIGENT_BUILTIN_AGENT_DIRS`` (or ``env_value``).

    Returns the ``os.pathsep``-separated entries that are directories containing a
    ``config.yaml`` (single-file specs and missing mounts are skipped — a bad operator
    path must never break seeding, mirroring ``_ensure_extra_builtin_agents``).
    """
    raw = env_value if env_value is not None else os.environ.get(BUNDLE_DIRS_ENV, "")
    dirs: list[Path] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        path = Path(entry)
        if path.is_dir() and (path / "config.yaml").is_file():
            dirs.append(path)
    return dirs


def parse_workflow_bundle(bundle_dir: Path) -> WorkflowBundle | None:
    """Parse a bundle dir's ``config.yaml``; return a :class:`WorkflowBundle` iff it is
    a ``params.workflow: true`` orchestrator, else ``None``.

    Best-effort: an unreadable / malformed spec is logged and skipped (returns ``None``)
    so one bad bundle never aborts the seed.
    """
    config_path = bundle_dir / "config.yaml"
    try:
        spec = yaml.safe_load(config_path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001 — one bad bundle must not abort the seed
        logger.warning("workflow-task seed: failed to parse %s: %s", config_path, exc)
        return None
    if not isinstance(spec, dict):
        return None
    params = spec.get("params") or {}
    if not isinstance(params, dict) or not params.get("workflow"):
        return None
    orchestrator = params.get("orchestrator")
    department = params.get("department")
    if not orchestrator or not department:
        logger.warning(
            "workflow-task seed: %s is workflow:true but missing "
            "orchestrator/department; skipping",
            bundle_dir.name,
        )
        return None
    name = str(spec.get("name") or bundle_dir.name)
    description = str(spec.get("description") or "").strip()
    prompt = str(spec.get("prompt") or "")
    return WorkflowBundle(
        name=name,
        orchestrator=str(orchestrator),
        department=str(department),
        description=description,
        cadence=(str(params["cadence"]) if params.get("cadence") else None),
        specialists=_extract_specialists(spec),
        intent=_first_lines(prompt, limit=3),
    )


def _extract_specialists(spec: dict) -> tuple[str, ...]:
    """Pull the orchestrator's allowed specialist roster from the bundle.

    Reads the ``allowed_subagents`` guardrail's ``allowed_agents`` argument — the set of
    team agents the workflow may launch — so the derived capabilities reflect the real
    delegation surface. Returns ``()`` when the guardrail is absent.
    """
    try:
        policies = spec["guardrails"]["policies"]
        allowed = policies["allowed_subagents"]["function"]["arguments"]["allowed_agents"]
    except (KeyError, TypeError):
        return ()
    if not isinstance(allowed, list):
        return ()
    return tuple(str(a) for a in allowed if a)


def _first_lines(text: str, *, limit: int) -> str:
    """First ``limit`` non-empty, stripped lines of ``text`` joined by spaces (bounded)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " ".join(lines[:limit])[:600]


@dataclass(frozen=True)
class WorkflowTaskSeed:
    """The deterministic Task row derived from one workflow bundle."""

    id: str
    title: str
    owner_agent_id: str
    assignee_agent_id: str
    required_capability: str
    priority: int
    payload: dict[str, Any]


def derive_capabilities(bundle: WorkflowBundle) -> list[str]:
    """The capability set for a workflow Task: the department (always) plus each
    specialist the orchestrator delegates to, de-duplicated and slugged, order-stable.

    The department slug is the *gating* ``required_capability``; the full set lives in
    the payload so a richer match can fold in the delegation surface.
    """
    caps: list[str] = [_slug(bundle.department)]
    for specialist in bundle.specialists:
        slug = _slug(specialist)
        if slug and slug not in caps:
            caps.append(slug)
    return caps


def build_task_seed(bundle: WorkflowBundle) -> WorkflowTaskSeed:
    """Build the deterministic :class:`WorkflowTaskSeed` for a bundle.

    ``owner`` == ``assignee`` == the bundle's orchestrator (it is accountable for AND
    runs the workflow); ``required_capability`` == the department slug; ``payload``
    carries the orchestration intent (name, description, roster, cadence, capabilities)
    so running the Task is meaningful. The id is ``task_wf_<bundle-name>`` for idempotency.
    """
    department_slug = _slug(bundle.department)
    capabilities = derive_capabilities(bundle)
    payload = {
        "kind": "workflow-orchestrator",
        "bundle": bundle.name,
        "orchestrator": bundle.orchestrator,
        "department": bundle.department,
        "department_slug": department_slug,
        "cadence": bundle.cadence,
        "capabilities": capabilities,
        "specialists": list(bundle.specialists),
        "description": bundle.description,
        "intent": bundle.intent,
    }
    return WorkflowTaskSeed(
        id=f"task_wf_{bundle.name}",
        title=bundle.description or bundle.name,
        owner_agent_id=bundle.orchestrator,
        assignee_agent_id=bundle.orchestrator,
        required_capability=department_slug,
        priority=3,
        payload=payload,
    )


def _upsert_seed(session_maker, seed: WorkflowTaskSeed, now: int) -> str:
    """Insert or refresh one workflow Task row (the idempotent unit).

    On insert the row starts ``open``. On update only the template-derived fields
    refresh — the runtime ``status`` is left untouched so a re-seed never resets an
    orchestrator task that a run has already advanced.
    """
    payload_json = json.dumps(seed.payload, sort_keys=True)
    with session_maker() as session:
        row = session.get(SqlTask, seed.id)
        if row is None:
            session.add(
                SqlTask(
                    id=seed.id,
                    title=seed.title,
                    owner_agent_id=seed.owner_agent_id,
                    assignee_agent_id=seed.assignee_agent_id,
                    required_capability=seed.required_capability,
                    status="open",
                    priority=seed.priority,
                    source=SEED_SOURCE,
                    payload=payload_json,
                    created_at=now,
                    updated_at=now,
                )
            )
            return "inserted"
        derived = {
            "title": seed.title,
            "owner_agent_id": seed.owner_agent_id,
            "assignee_agent_id": seed.assignee_agent_id,
            "required_capability": seed.required_capability,
            "priority": seed.priority,
            "source": SEED_SOURCE,
            "payload": payload_json,
        }
        changed = False
        for attr, value in derived.items():
            if getattr(row, attr) != value:
                setattr(row, attr, value)
                changed = True
        if changed:
            row.updated_at = now
        return "updated" if changed else "unchanged"


def seed_workflow_tasks(
    *,
    store=None,
    bundle_dirs: list[Path] | None = None,
    env_value: str | None = None,
    now: int | None = None,
) -> int:
    """Upsert one Task per ``params.workflow: true`` bundle; return the count seeded.

    Idempotent: re-running converges on the same row set (stable ids). ``store`` defaults
    to :func:`bytedesk_omnigent.tasks.get_task_store`; ``bundle_dirs`` defaults to the
    dirs in ``OMNIGENT_BUILTIN_AGENT_DIRS`` (``env_value`` overrides for tests).
    """
    if store is None:
        from bytedesk_omnigent.tasks import get_task_store

        store = get_task_store()
    if bundle_dirs is None:
        bundle_dirs = workflow_bundle_dirs(env_value)
    now = now_epoch() if now is None else now

    session_maker = make_managed_session_maker(store.engine, immediate=True)
    inserted = updated = unchanged = 0
    for bundle_dir in bundle_dirs:
        bundle = parse_workflow_bundle(bundle_dir)
        if bundle is None:
            continue
        outcome = _upsert_seed(session_maker, build_task_seed(bundle), now)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
        else:
            unchanged += 1

    total = inserted + updated + unchanged
    logger.info(
        "workflow-task seed: %d workflow task(s) (%d new, %d refreshed, %d unchanged)",
        total,
        inserted,
        updated,
        unchanged,
    )
    return total
