"""Schedules admin API for durable wake-up triggers."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bytedesk_omnigent.scheduler.scheduler import CronTrigger, compute_next_fire
from omnigent.db.utils import now_epoch
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


class CadenceDraftBody(BaseModel):
    """Natural-language cadence text to convert into a schedule expression."""

    natural_language: str = Field(min_length=1, max_length=500)


class CreateScheduleBody(BaseModel):
    """Create a durable wake-up trigger for an agent/task."""

    agent_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=240)
    task_id: str | None = Field(default=None, max_length=128)
    prompt: str | None = Field(default=None, max_length=12000)
    schedule_kind: Literal["interval", "cron", "once"] | None = None
    schedule_expr: str | None = Field(default=None, max_length=128)
    natural_language: str | None = Field(default=None, max_length=500)
    start_at: datetime | None = None
    timezone: str = Field(default="UTC", max_length=80)


class ScheduleEnabledBody(BaseModel):
    """Enable/disable a schedule."""

    enabled: bool


_DOW = {
    "sun": 0,
    "sunday": 0,
    "mon": 1,
    "monday": 1,
    "tue": 2,
    "tuesday": 2,
    "wed": 3,
    "wednesday": 3,
    "thu": 4,
    "thursday": 4,
    "fri": 5,
    "friday": 5,
    "sat": 6,
    "saturday": 6,
}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "schedule"


def _epoch(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    return datetime.fromisoformat(raw)


def _parse_time(text: str) -> tuple[int, int]:
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text)
    if not match:
        return 9, 0
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3)
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("time must be within a 24-hour day")
    return hour, minute


def _derive_cadence(text: str) -> tuple[str, str]:
    """Small deterministic assistant for common cadence phrases."""
    value = text.strip().lower()
    if not value:
        raise ValueError("cadence text is required")

    interval = re.search(r"every\s+(\d+)\s*(minute|minutes|hour|hours|day|days)\b", value)
    if interval:
        amount = int(interval.group(1))
        unit = interval.group(2)
        multiplier = (
            60 if unit.startswith("minute") else 3600 if unit.startswith("hour") else 86400
        )
        return "interval", str(amount * multiplier)
    if value in {"hourly", "every hour"}:
        return "interval", "3600"

    hour, minute = _parse_time(value)
    if "weekday" in value or "business day" in value:
        return "cron", f"{minute} {hour} * * 1-5"
    for name, dow in _DOW.items():
        if re.search(rf"\b{name}\b", value):
            return "cron", f"{minute} {hour} * * {dow}"
    if "weekly" in value:
        return "cron", f"{minute} {hour} * * 1"
    if "daily" in value or "every day" in value:
        return "cron", f"{minute} {hour} * * *"
    raise ValueError(
        "unsupported cadence phrase; try 'daily at 9am', 'weekdays at 8:30am', "
        "'weekly on Monday at 10am', or 'every 30 minutes'"
    )


def _trigger_to_dict(trigger: CronTrigger) -> dict:
    data = asdict(trigger)
    data["schedule_kind"] = str(trigger.schedule_kind)
    payload = data.get("payload") or {}
    if isinstance(payload, dict):
        data["title"] = payload.get("title") or trigger.key
        data["task_id"] = payload.get("task_id")
        data["timezone"] = payload.get("timezone") or "UTC"
    else:
        data["title"] = trigger.key
        data["task_id"] = None
        data["timezone"] = "UTC"
    return data


def _project_occurrences(
    trigger: CronTrigger,
    *,
    start: int,
    end: int,
    limit: int,
) -> list[dict]:
    occurrences: list[dict] = []
    fire_at = trigger.next_fire_at
    cursor_guard = 0
    while fire_at < start and cursor_guard < limit:
        next_fire = compute_next_fire(str(trigger.schedule_kind), trigger.schedule_expr, fire_at)
        if next_fire is None or next_fire <= fire_at:
            break
        fire_at = next_fire
        cursor_guard += 1
    while start <= fire_at <= end and len(occurrences) < limit:
        payload = trigger.payload or {}
        occurrences.append(
            {
                "id": f"{trigger.id}:{fire_at}",
                "schedule_id": trigger.id,
                "agent_id": trigger.agent_id,
                "task_id": payload.get("task_id") if isinstance(payload, dict) else None,
                "title": payload.get("title") if isinstance(payload, dict) else trigger.key,
                "fire_at": fire_at,
            }
        )
        next_fire = compute_next_fire(str(trigger.schedule_kind), trigger.schedule_expr, fire_at)
        if next_fire is None or next_fire <= fire_at:
            break
        fire_at = next_fire
    return occurrences


def create_schedules_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the schedules admin router."""
    router = APIRouter()

    @router.post("/schedules/assistant/draft")
    async def draft_cadence(request: Request, body: CadenceDraftBody) -> JSONResponse:
        require_user(request, auth_provider)
        try:
            kind, expr = _derive_cadence(body.natural_language)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse({"schedule_kind": kind, "schedule_expr": expr})

    @router.get("/schedules")
    async def list_schedules(
        request: Request,
        agent_id: str | None = None,
        enabled: bool | None = None,
    ) -> JSONResponse:
        require_user(request, auth_provider)
        from bytedesk_omnigent.runtime import get_cron_scheduler

        schedules = get_cron_scheduler().list_triggers(agent_id=agent_id, enabled=enabled)
        return JSONResponse({"schedules": [_trigger_to_dict(s) for s in schedules]})

    @router.post("/schedules")
    async def create_schedule(request: Request, body: CreateScheduleBody) -> JSONResponse:
        require_user(request, auth_provider)
        from bytedesk_omnigent.runtime import get_cron_scheduler
        from bytedesk_omnigent.tasks.store import get_task_store

        schedule_kind = body.schedule_kind
        schedule_expr = body.schedule_expr
        if (not schedule_kind or not schedule_expr) and body.natural_language:
            schedule_kind, schedule_expr = _derive_cadence(body.natural_language)
        if not schedule_kind or not schedule_expr:
            raise HTTPException(
                status_code=422, detail="schedule_kind and schedule_expr are required"
            )
        if body.task_id and get_task_store().get_task(body.task_id) is None:
            raise HTTPException(status_code=404, detail="task not found")

        next_fire_at = _epoch(body.start_at) if body.start_at else None
        if schedule_kind == "once":
            if next_fire_at is None:
                parsed = _parse_iso(schedule_expr)
                if parsed is None:
                    raise HTTPException(status_code=422, detail="once schedules require start_at")
                next_fire_at = _epoch(parsed)
            schedule_expr = str(next_fire_at)

        payload = {
            "title": body.title.strip(),
            "task_id": body.task_id,
            "prompt": body.prompt.strip() if body.prompt else None,
            "run_as_agent_id": body.agent_id,
            "timezone": body.timezone,
        }
        key = f"schedule:{_slug(body.title)}:{uuid.uuid4().hex[:8]}"
        try:
            trigger = get_cron_scheduler().register_trigger(
                agent_id=body.agent_id,
                key=key,
                schedule_kind=schedule_kind,
                schedule_expr=schedule_expr,
                next_fire_at=next_fire_at,
                payload=payload,
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse({"schedule": _trigger_to_dict(trigger)}, status_code=201)

    @router.patch("/schedules/{schedule_id}")
    async def set_schedule_enabled(
        request: Request,
        schedule_id: str,
        body: ScheduleEnabledBody,
    ) -> JSONResponse:
        require_user(request, auth_provider)
        from bytedesk_omnigent.runtime import get_cron_scheduler

        ok = get_cron_scheduler().set_enabled(trigger_id=schedule_id, enabled=body.enabled)
        if not ok:
            raise HTTPException(status_code=404, detail="schedule not found")
        schedule = get_cron_scheduler().get_trigger(schedule_id)
        return JSONResponse({"schedule": _trigger_to_dict(schedule)} if schedule else {"ok": True})

    @router.get("/schedules/occurrences")
    async def list_occurrences(
        request: Request,
        agent_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = Query(default=500, ge=1, le=1000),
    ) -> JSONResponse:
        require_user(request, auth_provider)
        from bytedesk_omnigent.runtime import get_cron_scheduler

        now = datetime.now(tz=UTC)
        start_dt = _parse_iso(start) or now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = _parse_iso(end) or (start_dt + timedelta(days=7))
        start_epoch = _epoch(start_dt)
        end_epoch = _epoch(end_dt)
        if end_epoch < start_epoch:
            raise HTTPException(status_code=422, detail="end must be after start")

        occurrences: list[dict] = []
        for trigger in get_cron_scheduler().list_triggers(agent_id=agent_id, enabled=True):
            if len(occurrences) >= limit:
                break
            occurrences.extend(
                _project_occurrences(
                    trigger,
                    start=start_epoch,
                    end=end_epoch,
                    limit=limit - len(occurrences),
                )
            )
        occurrences.sort(key=lambda item: item["fire_at"])
        return JSONResponse({"occurrences": occurrences[:limit], "now": now_epoch()})

    return router
