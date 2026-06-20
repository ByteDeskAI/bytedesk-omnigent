"""Deterministic third-party integration backfill plan compiler.

Backfills let Omnigent agents ingest historical records from systems like Slack,
Notion, GitHub, Linear, and Google Workspace before future webhook events arrive.
This module deliberately compiles a read-only, bounded, resumable plan instead of
letting a runner improvise pagination, checkpointing, and idempotency at runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_READ_SCOPE_MARKERS = ("read", "history", "metadata", "list", "search")
_WRITE_SCOPE_MARKERS = (
    "write",
    "delete",
    "admin",
    "manage",
    "modify",
    "create",
    "update",
    "send",
    "post",
)


@dataclass(frozen=True)
class IntegrationBackfillRequest:
    """Inputs for a deterministic historical-sync/backfill plan.

    :param source: Connector/source name, e.g. ``slack`` or ``github``.
    :param resource: Resource stream, e.g. ``channel.messages`` or ``issues``.
    :param workspace_id: Tenant/workspace/account partition used in checkpoints.
    :param start_cursor: First cursor/timestamp/id to fetch from.
    :param end_cursor: Optional upper bound; omitted means "until connector exhausts".
    :param page_size: Maximum items per connector page.
    :param max_pages: Hard page bound for this run; keeps autonomous work bounded.
    :param required_scopes: OAuth/API scopes the backfill runner needs; must be read-only.
    """

    source: str
    resource: str
    workspace_id: str
    start_cursor: str
    end_cursor: str | None = None
    page_size: int = 100
    max_pages: int = 10
    required_scopes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BackfillStep:
    """One deterministic action in an integration backfill run."""

    name: str
    action: str
    cursor: str | None = None
    page_size: int | None = None
    checkpoint_after: str | None = None
    signal_payload: dict[str, object] | None = None


@dataclass(frozen=True)
class IntegrationBackfillPlan:
    """Compiled plan a runner can execute without inventing integration policy."""

    source: str
    resource: str
    workspace_id: str
    checkpoint_key: str
    idempotency_scope: str
    completion_match_key: str
    required_scopes: tuple[str, ...]
    safety_notes: tuple[str, ...]
    steps: tuple[BackfillStep, ...]


def compile_backfill_plan(request: IntegrationBackfillRequest) -> IntegrationBackfillPlan:
    """Compile a bounded, resumable, read-only third-party backfill plan."""
    source = _slug(request.source)
    resource = _resource_slug(request.resource)
    workspace_id = _slug(request.workspace_id)
    if not source:
        raise ValueError("source is required")
    if not resource:
        raise ValueError("resource is required")
    if not workspace_id:
        raise ValueError("workspace_id is required")
    if not request.start_cursor:
        raise ValueError("start_cursor is required")
    if not 1 <= request.max_pages <= 100:
        raise ValueError("max_pages must be between 1 and 100")
    if not 1 <= request.page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")
    _ensure_read_only_scopes(request.required_scopes)

    checkpoint_key = f"integration-backfill:{workspace_id}:{source}:{resource}"
    idempotency_scope = f"integration-backfill/{source}/{resource}"
    completion_match_key = f"{resource}.backfill.completed"

    steps: list[BackfillStep] = [
        BackfillStep(
            name="load_checkpoint",
            action="load the last committed checkpoint, falling back to start_cursor",
            cursor=request.start_cursor,
        )
    ]
    for page in range(1, request.max_pages + 1):
        steps.append(
            BackfillStep(
                name=f"fetch_page_{page}",
                action=(
                    f"fetch up to {request.page_size} {source}/{resource} records "
                    "from the current checkpoint cursor"
                ),
                cursor=request.start_cursor if page == 1 else f"page:{page - 1}",
                page_size=request.page_size,
            )
        )
        steps.append(
            BackfillStep(
                name=f"commit_page_{page}",
                action=(
                    "deduplicate imported records, create/refresh Omnigent work items, "
                    "then persist the next checkpoint"
                ),
                checkpoint_after=f"page:{page}",
            )
        )
    steps.append(
        BackfillStep(
            name="emit_completion_event",
            action="emit the completion match key so waiting workflows can resume",
            signal_payload={
                "source": source,
                "resource": resource,
                "workspace_id": workspace_id,
                "checkpoint_key": checkpoint_key,
                "pages_planned": request.max_pages,
            },
        )
    )

    return IntegrationBackfillPlan(
        source=source,
        resource=resource,
        workspace_id=workspace_id,
        checkpoint_key=checkpoint_key,
        idempotency_scope=idempotency_scope,
        completion_match_key=completion_match_key,
        required_scopes=tuple(request.required_scopes),
        safety_notes=(
            "read-only connector scope; never mutate third-party records during backfill",
            "persist the checkpoint after each committed page before fetching the next page",
            "deduplicate every imported record through the idempotency scope before task creation",
        ),
        steps=tuple(steps),
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _resource_slug(value: str) -> str:
    return ".".join(part for part in (_slug(part) for part in value.split(".")) if part)


def _ensure_read_only_scopes(scopes: tuple[str, ...]) -> None:
    for scope in scopes:
        normalized = scope.strip().lower()
        tokens = [token for token in re.split(r"[^a-z0-9]+", normalized) if token]
        if any(marker in tokens for marker in _WRITE_SCOPE_MARKERS):
            raise ValueError("backfill plans only allow read-only scopes")
        if tokens and not any(marker in tokens for marker in _READ_SCOPE_MARKERS):
            raise ValueError("backfill plans only allow read-only scopes")
