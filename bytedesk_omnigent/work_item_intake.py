"""External work-item intake for integration-backed autonomous tasks.

This module is the deterministic seam between third-party work trackers
(GitHub, Linear, Jira, Trello, and generic OAuth/service integrations) and the
Omnigent Task backlog. It intentionally performs no network calls and requires no
live credentials: authenticated webhooks, OAuth callbacks, or platform adapters
can hand a provider payload to this normalizer and receive an idempotent Task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from bytedesk_omnigent.tasks.store import Task


@dataclass(frozen=True)
class WorkItemDraft:
    """Normalized work item ready to become an Omnigent Task."""

    provider: str
    external_id: str
    title: str
    url: str | None
    body: str | None
    labels: tuple[str, ...]
    priority: int
    required_capability: str


@dataclass(frozen=True)
class WorkItemIngestResult:
    """Result of ingesting a third-party work item into the Task backlog."""

    task: Task
    created: bool
    draft: WorkItemDraft


class WorkItemTaskStore(Protocol):
    """TaskStore subset needed by the deterministic work-item intake seam."""

    def create_task(
        self,
        *,
        title: str,
        priority: int = 3,
        source: str | None = None,
        required_capability: str | None = None,
        payload: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> Task:
        ...

    def list_tasks(
        self,
        *,
        status: str | None = None,
        owner_agent_id: str | None = None,
        assignee_agent_id: str | None = None,
    ) -> list[Task]:
        ...


def normalize_work_item(payload: dict[str, Any], *, source: str | None = None) -> WorkItemDraft:
    """Normalize a provider payload into a deterministic Task draft.

    ``source`` may be supplied by the route/query path. When omitted, the payload
    can carry ``provider`` or ``source``. Unknown sources still normalize through a
    generic work-item shape so new integrations can start with a safe task-intake
    bridge before a bespoke adapter lands.
    """

    provider = _clean_provider(source or _str(payload.get("provider") or payload.get("source")))
    if provider == "github":
        return _normalize_github(payload)
    if provider == "linear":
        return _normalize_linear(payload)
    if provider == "jira":
        return _normalize_jira(payload)
    if provider == "trello":
        return _normalize_trello(payload)
    return _normalize_generic(payload, provider=provider or "generic")


def ingest_work_item(
    *,
    payload: dict[str, Any],
    store: WorkItemTaskStore,
    source: str | None = None,
    now: int | None = None,
) -> WorkItemIngestResult:
    """Create or return an existing Task for one external work item.

    Idempotency is keyed by ``provider`` + ``external_id`` in the Task payload.
    This avoids a schema migration while still making webhook retries and repeated
    platform syncs safe.
    """

    draft = normalize_work_item(payload, source=source)
    existing = find_existing_work_item_task(store=store, draft=draft)
    if existing is not None:
        return WorkItemIngestResult(task=existing, created=False, draft=draft)

    task_payload = {
        "kind": "external_work_item",
        "provider": draft.provider,
        "external_id": draft.external_id,
        "url": draft.url,
        "body": draft.body,
        "labels": list(draft.labels),
        "raw": payload,
    }
    task = store.create_task(
        title=draft.title,
        priority=draft.priority,
        source=f"work_item:{draft.provider}",
        required_capability=draft.required_capability,
        payload=task_payload,
        now=now,
    )
    return WorkItemIngestResult(task=task, created=True, draft=draft)


def find_existing_work_item_task(
    *, store: WorkItemTaskStore, draft: WorkItemDraft
) -> Task | None:
    """Return the existing Task for ``draft`` if this work item was already synced."""

    for task in store.list_tasks():
        if task.source != f"work_item:{draft.provider}":
            continue
        payload = task.payload or {}
        if (
            payload.get("kind") == "external_work_item"
            and payload.get("provider") == draft.provider
            and payload.get("external_id") == draft.external_id
        ):
            return task
    return None


def _normalize_github(payload: dict[str, Any]) -> WorkItemDraft:
    item = _dict(payload.get("issue")) or _dict(payload.get("pull_request")) or payload
    number = _str(item.get("number"))
    external_id = _first_str(item.get("id"), number, item.get("node_id"), item.get("html_url"))
    title = _first_str(item.get("title"), f"GitHub work item {external_id}")
    labels = tuple(
        label
        for label in (_label_name(label) for label in _list(item.get("labels")))
        if label
    )
    return WorkItemDraft(
        provider="github",
        external_id=external_id,
        title=title,
        url=_maybe_str(item.get("html_url") or item.get("url")),
        body=_maybe_str(item.get("body")),
        labels=labels,
        priority=_priority_from_labels(labels, default=2),
        required_capability="developer.work_item",
    )


def _normalize_linear(payload: dict[str, Any]) -> WorkItemDraft:
    item = _dict(payload.get("data")) or payload
    external_id = _first_str(item.get("id"), item.get("identifier"), item.get("url"))
    title = _first_str(item.get("title"), f"Linear issue {external_id}")
    labels = tuple(_linear_labels(item))
    return WorkItemDraft(
        provider="linear",
        external_id=external_id,
        title=title,
        url=_maybe_str(item.get("url")),
        body=_maybe_str(item.get("description")),
        labels=labels,
        priority=_priority_from_provider_value(item.get("priority"), labels=labels),
        required_capability="project_management.work_item",
    )


def _normalize_jira(payload: dict[str, Any]) -> WorkItemDraft:
    item = _dict(payload.get("issue")) or payload
    fields = _dict(item.get("fields"))
    external_id = _first_str(item.get("key"), item.get("id"), item.get("self"))
    title = _first_str(fields.get("summary"), item.get("summary"), f"Jira issue {external_id}")
    raw_labels = _list(fields.get("labels") or item.get("labels"))
    labels = tuple(_str(label) for label in raw_labels if _str(label))
    return WorkItemDraft(
        provider="jira",
        external_id=external_id,
        title=title,
        url=_maybe_str(item.get("self")),
        body=_maybe_str(fields.get("description") or item.get("description")),
        labels=labels,
        priority=_priority_from_name(
            _maybe_str(_dict(fields.get("priority")).get("name")),
            labels=labels,
        ),
        required_capability="project_management.work_item",
    )


def _normalize_trello(payload: dict[str, Any]) -> WorkItemDraft:
    action = _dict(payload.get("action"))
    data = _dict(action.get("data"))
    item = _dict(data.get("card")) or _dict(payload.get("card")) or payload
    external_id = _first_str(item.get("id"), item.get("shortLink"), item.get("url"))
    title = _first_str(item.get("name"), item.get("title"), f"Trello card {external_id}")
    labels = tuple(_label_name(label) for label in _list(item.get("labels")) if _label_name(label))
    return WorkItemDraft(
        provider="trello",
        external_id=external_id,
        title=title,
        url=_maybe_str(item.get("url")),
        body=_maybe_str(item.get("desc") or item.get("description")),
        labels=labels,
        priority=_priority_from_labels(labels, default=4),
        required_capability="project_management.work_item",
    )


def _normalize_generic(payload: dict[str, Any], *, provider: str) -> WorkItemDraft:
    external_id = _first_str(
        payload.get("external_id"),
        payload.get("id"),
        payload.get("key"),
        payload.get("url"),
    )
    labels = tuple(_str(label) for label in _list(payload.get("labels")) if _str(label))
    return WorkItemDraft(
        provider=provider,
        external_id=external_id,
        title=_first_str(
            payload.get("title"),
            payload.get("name"),
            f"{provider.title()} work item {external_id}",
        ),
        url=_maybe_str(payload.get("url")),
        body=_maybe_str(payload.get("body") or payload.get("description")),
        labels=labels,
        priority=_priority_from_provider_value(payload.get("priority"), labels=labels),
        required_capability=_first_str(payload.get("required_capability"), "external.work_item"),
    )


def _priority_from_provider_value(value: Any, *, labels: tuple[str, ...]) -> int:
    text = _maybe_str(value)
    if text is None:
        return _priority_from_labels(labels, default=3)
    if text.isdigit():
        number = int(text)
        # Linear uses 1 urgent, 2 high, 3 medium, 4 low. Preserve that shape.
        return min(max(number, 1), 5)
    return _priority_from_name(text, labels=labels)


def _priority_from_name(name: str | None, *, labels: tuple[str, ...]) -> int:
    if name:
        lowered = name.lower()
        if lowered in {"urgent", "blocker", "critical", "highest", "p0"}:
            return 1
        if lowered in {"high", "p1"}:
            return 2
        if lowered in {"medium", "normal", "p2"}:
            return 3
        if lowered in {"low", "lowest", "p3", "p4"}:
            return 4
    return _priority_from_labels(labels, default=3)


def _priority_from_labels(labels: tuple[str, ...], *, default: int) -> int:
    lowered = {label.lower() for label in labels}
    if lowered & {"urgent", "blocker", "critical", "p0"}:
        return 1
    if lowered & {"high", "p1"}:
        return 2
    if lowered & {"low", "p3", "p4"}:
        return 4
    return default


def _linear_labels(item: dict[str, Any]) -> list[str]:
    labels = _list(item.get("labels"))
    if isinstance(item.get("label"), str):
        labels.append(item["label"])
    names: list[str] = []
    for label in labels:
        name = _label_name(label)
        if name:
            names.append(name)
    return names


def _label_name(label: Any) -> str:
    if isinstance(label, dict):
        return _str(label.get("name") or label.get("title") or label.get("id"))
    return _str(label)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_str(*values: Any) -> str:
    for value in values:
        text = _str(value)
        if text:
            return text
    raise ValueError("work item payload must include an id, title, or URL")


def _maybe_str(value: Any) -> str | None:
    text = _str(value)
    return text or None


def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_provider(value: str) -> str:
    return value.strip().lower().replace("_", "-")
