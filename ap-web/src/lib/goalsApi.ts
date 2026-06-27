import { authenticatedFetch } from "@/lib/identity";

export type GoalStatus = "open" | "assigned" | "in_progress" | "blocked" | "done";
export type GoalTargetKind = "organization" | "department" | "agent";
export type GoalReadinessKind = "immediate" | "dependent" | "deferred";
export type GoalActivationState = "ready" | "waiting" | "paused";
export type GoalDependencyStatus = "pending" | "satisfied" | "waived";
export type GoalDependencyKind = "manual" | "goal" | "system_state";

export interface GoalDependencyRecord {
  id: string;
  goal_id: string;
  kind: GoalDependencyKind;
  ref: string | null;
  label: string;
  status: GoalDependencyStatus;
  created_at: number;
  updated_at: number;
  resolved_at: number | null;
  metadata: Record<string, unknown> | null;
}

export interface GoalRecord {
  id: string;
  title: string;
  owner_agent_id: string | null;
  status: GoalStatus;
  priority: number;
  source: string | null;
  payload: Record<string, unknown> | null;
  created_at: number;
  updated_at: number;
  target_kind: GoalTargetKind;
  target_id: string;
  target_label: string | null;
  readiness_kind: GoalReadinessKind;
  activation_state: GoalActivationState;
  dependencies: GoalDependencyRecord[];
}

export interface GoalFilters {
  status?: GoalStatus;
  owner?: string;
  target_kind?: GoalTargetKind;
  target_id?: string;
  readiness_kind?: GoalReadinessKind;
  activation_state?: GoalActivationState;
  ready_only?: boolean;
  include_dependencies?: boolean;
}

export interface CreateGoalRequest {
  title: string;
  priority?: number;
  source?: string | null;
  payload?: Record<string, unknown> | null;
  target_kind?: GoalTargetKind;
  target_id?: string | null;
  target_label?: string | null;
  readiness_kind?: GoalReadinessKind;
  dependencies?: Array<{
    kind?: GoalDependencyKind;
    ref?: string | null;
    label: string;
    status?: GoalDependencyStatus;
    metadata?: Record<string, unknown> | null;
  }>;
}

export interface UpdateGoalRequest {
  title?: string;
  priority?: number;
  payload?: Record<string, unknown> | null;
  status?: GoalStatus;
  target_kind?: GoalTargetKind;
  target_id?: string | null;
  target_label?: string | null;
  readiness_kind?: GoalReadinessKind;
  activation_state?: GoalActivationState;
}

export interface CreateGoalDependencyRequest {
  kind?: GoalDependencyKind;
  ref?: string | null;
  label: string;
  status?: GoalDependencyStatus;
  metadata?: Record<string, unknown> | null;
}

export interface UpdateGoalDependencyRequest {
  kind?: GoalDependencyKind;
  ref?: string | null;
  label?: string;
  status?: GoalDependencyStatus;
  metadata?: Record<string, unknown> | null;
}

export interface GoalPlannerSource {
  id: string;
  label: string;
  available: boolean;
  tools: string[];
  reason?: string | null;
}

export interface StartGoalPlanningSessionRequest {
  target_kind: GoalTargetKind;
  target_id: string;
  target_label?: string | null;
  source_ids?: string[];
}

export interface GoalPlanningSession {
  session_id: string;
  agent_id: string;
  agent_name: string;
  title: string;
  prompt: string;
  sources: GoalPlannerSource[];
  web_path: string;
}

export interface GoalDraft {
  title: string;
  priority?: number;
  target_kind?: GoalTargetKind;
  target_id?: string | null;
  target_label?: string | null;
  readiness_kind?: GoalReadinessKind;
  dependencies?: CreateGoalDependencyRequest[];
  outcome?: string | null;
  acceptance_criteria?: string[];
  assumptions?: string[];
  source_refs?: Record<string, unknown>[];
  payload?: Record<string, unknown> | null;
}

export interface CommitGoalPlanningSessionRequest {
  source_ids?: string[];
  draft: GoalDraft;
}

export interface GoalTemplate {
  id: string;
  name: string;
  description: string | null;
  definition: Record<string, unknown>;
  created_at: number;
  updated_at: number;
}

export interface CreateGoalTemplateRequest {
  name: string;
  description?: string | null;
  definition?: Record<string, unknown>;
}

export interface UpdateGoalTemplateRequest {
  name?: string;
  description?: string | null;
  definition?: Record<string, unknown>;
}

export interface InstantiateGoalTemplateRequest {
  overrides?: Record<string, unknown>;
}

async function readJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail =
      typeof body.detail === "string"
        ? body.detail
        : typeof body.error?.message === "string"
          ? body.error.message
          : `${res.status} ${res.statusText}`;
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

function goalQuery(filters: GoalFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === null || value === false) continue;
    params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export async function listGoals(filters: GoalFilters = {}): Promise<GoalRecord[]> {
  const res = await authenticatedFetch(`/v1/goals${goalQuery(filters)}`);
  const body = await readJson<{ goals: GoalRecord[] }>(res);
  return body.goals;
}

export async function getGoal(goalId: string): Promise<GoalRecord> {
  const res = await authenticatedFetch(`/v1/goals/${encodeURIComponent(goalId)}`);
  const body = await readJson<{ goal: GoalRecord }>(res);
  return body.goal;
}

export async function createGoal(payload: CreateGoalRequest): Promise<GoalRecord> {
  const res = await authenticatedFetch("/v1/goals", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await readJson<{ goal: GoalRecord }>(res);
  return body.goal;
}

export async function listGoalPlannerSources(): Promise<GoalPlannerSource[]> {
  const res = await authenticatedFetch("/v1/goals/planner/sources");
  const body = await readJson<{ sources: GoalPlannerSource[] }>(res);
  return body.sources;
}

export async function startGoalPlanningSession(
  payload: StartGoalPlanningSessionRequest,
): Promise<GoalPlanningSession> {
  const res = await authenticatedFetch("/v1/goals/planner/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return readJson<GoalPlanningSession>(res);
}

export async function commitGoalPlanningSession(
  sessionId: string,
  payload: CommitGoalPlanningSessionRequest,
): Promise<GoalRecord> {
  const res = await authenticatedFetch(
    `/v1/goals/planner/sessions/${encodeURIComponent(sessionId)}/commit`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  const body = await readJson<{ goal: GoalRecord }>(res);
  return body.goal;
}

export async function updateGoal(
  goalId: string,
  payload: UpdateGoalRequest,
): Promise<GoalRecord> {
  const res = await authenticatedFetch(`/v1/goals/${encodeURIComponent(goalId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await readJson<{ goal: GoalRecord }>(res);
  return body.goal;
}

export async function activateGoal(goalId: string): Promise<GoalRecord> {
  const res = await authenticatedFetch(`/v1/goals/${encodeURIComponent(goalId)}/activate`, {
    method: "POST",
  });
  const body = await readJson<{ goal: GoalRecord }>(res);
  return body.goal;
}

export async function addGoalDependency(
  goalId: string,
  payload: CreateGoalDependencyRequest,
): Promise<GoalDependencyRecord> {
  const res = await authenticatedFetch(`/v1/goals/${encodeURIComponent(goalId)}/dependencies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await readJson<{ dependency: GoalDependencyRecord }>(res);
  return body.dependency;
}

export async function updateGoalDependency(
  goalId: string,
  dependencyId: string,
  payload: UpdateGoalDependencyRequest,
): Promise<GoalDependencyRecord> {
  const res = await authenticatedFetch(
    `/v1/goals/${encodeURIComponent(goalId)}/dependencies/${encodeURIComponent(dependencyId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  const body = await readJson<{ dependency: GoalDependencyRecord }>(res);
  return body.dependency;
}

export async function deleteGoal(goalId: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/goals/${encodeURIComponent(goalId)}`, {
    method: "DELETE",
  });
  await readJson<{ deleted: string }>(res);
}

export async function deleteGoalDependency(
  goalId: string,
  dependencyId: string,
): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/goals/${encodeURIComponent(goalId)}/dependencies/${encodeURIComponent(dependencyId)}`,
    { method: "DELETE" },
  );
  await readJson<{ deleted: string }>(res);
}

export async function listGoalTemplates(): Promise<GoalTemplate[]> {
  const res = await authenticatedFetch("/v1/goal-templates");
  const body = await readJson<{ templates: GoalTemplate[] }>(res);
  return body.templates;
}

export async function createGoalTemplate(
  payload: CreateGoalTemplateRequest,
): Promise<GoalTemplate> {
  const res = await authenticatedFetch("/v1/goal-templates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await readJson<{ template: GoalTemplate }>(res);
  return body.template;
}

export async function updateGoalTemplate(
  templateId: string,
  payload: UpdateGoalTemplateRequest,
): Promise<GoalTemplate> {
  const res = await authenticatedFetch(
    `/v1/goal-templates/${encodeURIComponent(templateId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  const body = await readJson<{ template: GoalTemplate }>(res);
  return body.template;
}

export async function deleteGoalTemplate(templateId: string): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/goal-templates/${encodeURIComponent(templateId)}`,
    { method: "DELETE" },
  );
  await readJson<{ deleted: string }>(res);
}

export async function instantiateGoalTemplate(
  templateId: string,
  payload: InstantiateGoalTemplateRequest = {},
): Promise<GoalRecord> {
  const res = await authenticatedFetch(
    `/v1/goal-templates/${encodeURIComponent(templateId)}/instantiate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  const body = await readJson<{ goal: GoalRecord }>(res);
  return body.goal;
}
