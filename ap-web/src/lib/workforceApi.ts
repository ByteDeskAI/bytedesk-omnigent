import { authenticatedFetch } from "@/lib/identity";

export type WorkforceScopeKind = "organization" | "department";
export type WorkforceItemKind = "connector" | "skill";

export interface WorkforceScopeSummary {
  scopeKind: WorkforceScopeKind;
  scopeId: string;
  label: string;
  agentIds: string[];
}

export interface WorkforceInstruction {
  id: string;
  scopeKind: "organization" | "department" | "agent";
  scopeId: string;
  body: string;
  enabled: boolean;
  createdAt: number;
  updatedAt: number;
  version: number;
  metadata: Record<string, unknown>;
}

export interface WorkforceConnectorAssignment {
  id: string;
  scopeKind: WorkforceScopeKind;
  scopeId: string;
  connectionId: string;
  serviceKey: string;
  toolKey: string;
  itemKey: string;
  enabled: boolean;
  createdAt: number;
  updatedAt: number;
  version: number;
  metadata: Record<string, unknown>;
}

export interface WorkforceSkillAssignment {
  id: string;
  scopeKind: WorkforceScopeKind;
  scopeId: string;
  skillName: string;
  source: string;
  sourceRef: string | null;
  itemKey: string;
  enabled: boolean;
  createdAt: number;
  updatedAt: number;
  version: number;
  metadata: Record<string, unknown>;
}

export interface WorkforceAgentOverride {
  id: string;
  agentId: string;
  itemKind: WorkforceItemKind;
  itemKey: string;
  enabled: boolean;
  createdAt: number;
  updatedAt: number;
  version: number;
  metadata: Record<string, unknown>;
}

export interface WorkforceAgentMaterialization {
  id: string;
  agentId: string;
  itemKind: WorkforceItemKind;
  itemKey: string;
  active: boolean;
  createdAt: number;
  updatedAt: number;
  metadata: Record<string, unknown>;
}

export interface WorkforceScopeDetail {
  scopeKind: WorkforceScopeKind;
  scopeId: string;
  instruction: WorkforceInstruction | null;
  connectors: WorkforceConnectorAssignment[];
  skills: WorkforceSkillAssignment[];
  revision: number;
}

export interface WorkforceScopesResponse {
  scopes: WorkforceScopeSummary[];
  revision: number;
}

export interface WorkforceEffectiveConnector {
  itemKey: string;
  connectionId: string;
  serviceKey: string;
  toolKey: string;
  enabled: boolean;
  inherited: boolean;
  inheritedFrom: WorkforceConnectorAssignment[];
  override: WorkforceAgentOverride | null;
}

export interface WorkforceEffectiveSkill {
  itemKey: string;
  skillName: string;
  source: string;
  sourceRef: string | null;
  enabled: boolean;
  inherited: boolean;
  inheritedFrom: WorkforceSkillAssignment[];
  override: WorkforceAgentOverride | null;
}

export interface WorkforceEffectiveAgent {
  agentId: string;
  found: boolean;
  category?: string;
  department?: string | null;
  departmentSlug?: string | null;
  revision?: number;
  instructions?: WorkforceInstruction[];
  connectors?: WorkforceEffectiveConnector[];
  skills?: WorkforceEffectiveSkill[];
  overrides?: WorkforceAgentOverride[];
  materializations?: WorkforceAgentMaterialization[];
}

export interface UpsertWorkforceConnectorPayload {
  connectionId: string;
  tools: string[];
  enabled?: boolean;
  replace?: boolean;
  reconcile?: boolean;
  materialize?: boolean;
}

export interface UpsertWorkforceSkillPayload {
  skillName: string;
  source?: string;
  sourceRef?: string | null;
  enabled?: boolean;
  reconcile?: boolean;
}

export interface UpsertWorkforceOverridePayload {
  itemKind: WorkforceItemKind;
  itemKey: string;
  enabled: boolean;
  reconcile?: boolean;
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (res.ok) return (await res.json()) as T;
  try {
    const body = (await res.json()) as { error?: { message?: string }; message?: string };
    throw new Error(body.error?.message ?? body.message ?? `${res.status} ${res.statusText}`);
  } catch (err) {
    if (err instanceof Error && err.message) throw err;
    throw new Error(`${res.status} ${res.statusText}`, { cause: err });
  }
}

function scopePath(scopeKind: WorkforceScopeKind, scopeId?: string | null): string {
  if (scopeKind === "organization") return "/v1/workforce/scopes/organization";
  return `/v1/workforce/scopes/department/${encodeURIComponent(scopeId ?? "")}`;
}

export async function fetchWorkforceScopes(): Promise<WorkforceScopesResponse> {
  const res = await authenticatedFetch("/v1/workforce/scopes");
  return jsonOrThrow<WorkforceScopesResponse>(res);
}

export async function fetchWorkforceScope(
  scopeKind: WorkforceScopeKind,
  scopeId?: string | null,
): Promise<WorkforceScopeDetail> {
  const res = await authenticatedFetch(scopePath(scopeKind, scopeId));
  return jsonOrThrow<WorkforceScopeDetail>(res);
}

export async function updateWorkforceInstructions(
  scopeKind: WorkforceScopeKind,
  scopeId: string | null,
  body: string,
): Promise<{ instruction: WorkforceInstruction; scope: WorkforceScopeDetail }> {
  const res = await authenticatedFetch(`${scopePath(scopeKind, scopeId)}/instructions`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ body, enabled: true }),
  });
  return jsonOrThrow<{ instruction: WorkforceInstruction; scope: WorkforceScopeDetail }>(res);
}

export async function upsertWorkforceConnector(
  scopeKind: WorkforceScopeKind,
  scopeId: string | null,
  payload: UpsertWorkforceConnectorPayload,
): Promise<{
  assignments: WorkforceConnectorAssignment[];
  reconciledAgentIds: string[];
  scope: WorkforceScopeDetail;
}> {
  const res = await authenticatedFetch(`${scopePath(scopeKind, scopeId)}/connectors`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<{
    assignments: WorkforceConnectorAssignment[];
    reconciledAgentIds: string[];
    scope: WorkforceScopeDetail;
  }>(res);
}

export async function upsertWorkforceSkill(
  scopeKind: WorkforceScopeKind,
  scopeId: string | null,
  payload: UpsertWorkforceSkillPayload,
): Promise<{
  assignment: WorkforceSkillAssignment;
  reconciledAgentIds: string[];
  scope: WorkforceScopeDetail;
}> {
  const res = await authenticatedFetch(`${scopePath(scopeKind, scopeId)}/skills`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<{
    assignment: WorkforceSkillAssignment;
    reconciledAgentIds: string[];
    scope: WorkforceScopeDetail;
  }>(res);
}

export async function fetchWorkforceAgentEffective(
  agentId: string,
): Promise<WorkforceEffectiveAgent> {
  const res = await authenticatedFetch(
    `/v1/workforce/agents/${encodeURIComponent(agentId)}/effective`,
  );
  return jsonOrThrow<WorkforceEffectiveAgent>(res);
}

export async function updateWorkforceAgentInstructions(
  agentId: string,
  body: string,
): Promise<{ instruction: WorkforceInstruction; effective: WorkforceEffectiveAgent }> {
  const res = await authenticatedFetch(
    `/v1/workforce/agents/${encodeURIComponent(agentId)}/instructions`,
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ body, enabled: true }),
    },
  );
  return jsonOrThrow<{ instruction: WorkforceInstruction; effective: WorkforceEffectiveAgent }>(
    res,
  );
}

export async function upsertWorkforceAgentOverride(
  agentId: string,
  payload: UpsertWorkforceOverridePayload,
): Promise<{ override: WorkforceAgentOverride; effective: WorkforceEffectiveAgent }> {
  const res = await authenticatedFetch(
    `/v1/workforce/agents/${encodeURIComponent(agentId)}/overrides`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  return jsonOrThrow<{ override: WorkforceAgentOverride; effective: WorkforceEffectiveAgent }>(res);
}
