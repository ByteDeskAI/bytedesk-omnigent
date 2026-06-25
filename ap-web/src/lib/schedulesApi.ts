import { authenticatedFetch } from "@/lib/identity";

export interface TaskTemplate {
  id: string;
  title: string;
  owner_agent_id: string | null;
  assignee_agent_id: string | null;
  required_capability: string | null;
  status: string;
  priority: number;
  source: string | null;
  payload: Record<string, unknown> | null;
  created_at: number;
  updated_at: number;
}

export interface ScheduleRecord {
  id: string;
  agent_id: string;
  key: string;
  schedule_kind: "interval" | "cron" | "once";
  schedule_expr: string;
  next_fire_at: number;
  enabled: boolean;
  payload: Record<string, unknown> | null;
  version: number;
  title?: string;
  task_id?: string | null;
  timezone?: string;
}

export interface ScheduleOccurrence {
  id: string;
  schedule_id: string;
  agent_id: string;
  task_id: string | null;
  title: string;
  fire_at: number;
}

export interface CreateTaskRequest {
  title: string;
  prompt: string;
  owner_agent_id?: string | null;
  required_capability?: string | null;
  priority?: number;
  payload?: Record<string, unknown> | null;
}

export interface CreateScheduleRequest {
  agent_id: string;
  title: string;
  task_id?: string | null;
  prompt?: string | null;
  schedule_kind?: "interval" | "cron" | "once" | null;
  schedule_expr?: string | null;
  natural_language?: string | null;
  start_at?: string | null;
  timezone?: string;
}

async function readJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail =
      typeof body.detail === "string" ? body.detail : `${res.status} ${res.statusText}`;
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export async function listTasks(): Promise<TaskTemplate[]> {
  const res = await authenticatedFetch("/v1/tasks");
  const body = await readJson<{ tasks: TaskTemplate[] }>(res);
  return body.tasks;
}

export async function createTask(payload: CreateTaskRequest): Promise<TaskTemplate> {
  const res = await authenticatedFetch("/v1/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await readJson<{ task: TaskTemplate }>(res);
  return body.task;
}

export async function listSchedules(agentId?: string): Promise<ScheduleRecord[]> {
  const query = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  const res = await authenticatedFetch(`/v1/schedules${query}`);
  const body = await readJson<{ schedules: ScheduleRecord[] }>(res);
  return body.schedules;
}

export async function listScheduleOccurrences({
  agentId,
  start,
  end,
}: {
  agentId?: string;
  start: string;
  end: string;
}): Promise<ScheduleOccurrence[]> {
  const params = new URLSearchParams({ start, end });
  if (agentId) params.set("agent_id", agentId);
  const res = await authenticatedFetch(`/v1/schedules/occurrences?${params.toString()}`);
  const body = await readJson<{ occurrences: ScheduleOccurrence[] }>(res);
  return body.occurrences;
}

export async function createSchedule(payload: CreateScheduleRequest): Promise<ScheduleRecord> {
  const res = await authenticatedFetch("/v1/schedules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await readJson<{ schedule: ScheduleRecord }>(res);
  return body.schedule;
}

export async function draftCadence(naturalLanguage: string): Promise<{
  schedule_kind: "interval" | "cron" | "once";
  schedule_expr: string;
}> {
  const res = await authenticatedFetch("/v1/schedules/assistant/draft", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ natural_language: naturalLanguage }),
  });
  return readJson(res);
}
