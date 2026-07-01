import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchWorkforceAgentEffective,
  fetchWorkforceScope,
  fetchWorkforceScopes,
  fetchWorkforceToolCatalog,
  updateWorkforceAgentInstructions,
  updateWorkforceInstructions,
  upsertWorkforceAgentOverride,
  upsertWorkforceConnector,
  upsertWorkforceSkill,
  upsertWorkforceTool,
  type UpsertWorkforceConnectorPayload,
  type UpsertWorkforceOverridePayload,
  type UpsertWorkforceSkillPayload,
  type UpsertWorkforceToolPayload,
  type WorkforceScopeKind,
} from "@/lib/workforceApi";

const WORKFORCE_KEY = ["workforce"];

export function useWorkforceScopes() {
  return useQuery({
    queryKey: [...WORKFORCE_KEY, "scopes"],
    queryFn: fetchWorkforceScopes,
    staleTime: 10_000,
  });
}

export function useWorkforceToolCatalog() {
  return useQuery({
    queryKey: [...WORKFORCE_KEY, "tool-catalog"],
    queryFn: fetchWorkforceToolCatalog,
    staleTime: 60_000,
  });
}

export function useWorkforceScope(
  scopeKind: WorkforceScopeKind,
  scopeId?: string | null,
  enabled = true,
) {
  return useQuery({
    queryKey: [...WORKFORCE_KEY, "scope", scopeKind, scopeId ?? ""],
    queryFn: () => fetchWorkforceScope(scopeKind, scopeId),
    enabled: enabled && (scopeKind === "organization" || Boolean(scopeId)),
    staleTime: 10_000,
  });
}

export function useWorkforceAgentEffective(agentId?: string | null, enabled = true) {
  return useQuery({
    queryKey: [...WORKFORCE_KEY, "agent", agentId ?? ""],
    queryFn: () => fetchWorkforceAgentEffective(agentId ?? ""),
    enabled: enabled && Boolean(agentId),
    staleTime: 10_000,
  });
}

function invalidateWorkforce(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: WORKFORCE_KEY });
  void queryClient.invalidateQueries({ queryKey: ["connector-agent-grants"] });
  void queryClient.invalidateQueries({ queryKey: ["installed-skills"] });
  void queryClient.invalidateQueries({ queryKey: ["available-agents"] });
}

export function useUpdateWorkforceInstructions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      scopeKind,
      scopeId,
      body,
    }: {
      scopeKind: WorkforceScopeKind;
      scopeId?: string | null;
      body: string;
    }) => updateWorkforceInstructions(scopeKind, scopeId ?? null, body),
    onSuccess: () => invalidateWorkforce(queryClient),
  });
}

export function useUpdateWorkforceAgentInstructions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ agentId, body }: { agentId: string; body: string }) =>
      updateWorkforceAgentInstructions(agentId, body),
    onSuccess: () => invalidateWorkforce(queryClient),
  });
}

export function useUpsertWorkforceConnector() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      scopeKind,
      scopeId,
      ...payload
    }: {
      scopeKind: WorkforceScopeKind;
      scopeId?: string | null;
    } & UpsertWorkforceConnectorPayload) =>
      upsertWorkforceConnector(scopeKind, scopeId ?? null, payload),
    onSuccess: () => invalidateWorkforce(queryClient),
  });
}

export function useUpsertWorkforceSkill() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      scopeKind,
      scopeId,
      ...payload
    }: { scopeKind: WorkforceScopeKind; scopeId?: string | null } & UpsertWorkforceSkillPayload) =>
      upsertWorkforceSkill(scopeKind, scopeId ?? null, payload),
    onSuccess: () => invalidateWorkforce(queryClient),
  });
}

export function useUpsertWorkforceTool() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      scopeKind,
      scopeId,
      ...payload
    }: { scopeKind: WorkforceScopeKind; scopeId?: string | null } & UpsertWorkforceToolPayload) =>
      upsertWorkforceTool(scopeKind, scopeId ?? null, payload),
    onSuccess: () => invalidateWorkforce(queryClient),
  });
}

export function useUpsertWorkforceAgentOverride() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ agentId, ...payload }: { agentId: string } & UpsertWorkforceOverridePayload) =>
      upsertWorkforceAgentOverride(agentId, payload),
    onSuccess: () => invalidateWorkforce(queryClient),
  });
}
