import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export interface BlueprintGraphEdge {
  id: string;
  source: string;
  target: string;
}

export interface BlueprintGraphLoop {
  max_iterations: number;
  until?: unknown;
  on_exhausted: string;
  reuse_session: boolean;
  nodes: BlueprintGraphNode[];
  edges: BlueprintGraphEdge[];
}

export interface BlueprintGraphNode {
  id: string;
  kind: string;
  depends_on: string[];
  target?: string | null;
  when?: unknown;
  input?: unknown;
  return?: unknown;
  output?: unknown;
  metadata: Record<string, unknown>;
  loop?: BlueprintGraphLoop | null;
}

export interface BlueprintGraph {
  object: "blueprint";
  agent_id?: string | null;
  agent_name?: string | null;
  name?: string | null;
  description?: string | null;
  version: number;
  nodes: BlueprintGraphNode[];
  edges: BlueprintGraphEdge[];
  outputs: Record<string, unknown>;
}

export interface BlueprintRunNode {
  id: string;
  kind?: string | null;
  status?: string | null;
  loop_iteration?: number | null;
  child_session_id?: string | null;
  payload: Record<string, unknown>;
  updated_at?: number | null;
}

export interface BlueprintRun {
  object: "blueprint_run";
  blueprint_run_id?: string | null;
  status: string;
  nodes: BlueprintRunNode[];
  loop_iterations: Array<Record<string, unknown>>;
  events: Array<Record<string, unknown>>;
}

export function agentBlueprintQueryKey(agentId: string | null): readonly unknown[] {
  return ["agent", agentId, "blueprint"];
}

export function sessionBlueprintRunQueryKey(sessionId: string | null): readonly unknown[] {
  return ["session", sessionId, "blueprint-run"];
}

export async function fetchAgentBlueprint(agentId: string): Promise<BlueprintGraph | null> {
  const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(agentId)}/blueprint`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as BlueprintGraph;
}

export async function fetchSessionBlueprintRun(sessionId: string): Promise<BlueprintRun> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/blueprint-run`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as BlueprintRun;
}

export function useAgentBlueprint(agentId: string | null) {
  return useQuery({
    queryKey: agentBlueprintQueryKey(agentId),
    queryFn: () => fetchAgentBlueprint(agentId!),
    enabled: agentId !== null,
    staleTime: 60_000,
    retry: false,
  });
}

export function useSessionBlueprintRun(sessionId: string | null, pollMs?: number | null) {
  return useQuery({
    queryKey: sessionBlueprintRunQueryKey(sessionId),
    queryFn: () => fetchSessionBlueprintRun(sessionId!),
    enabled: sessionId !== null,
    staleTime: 1_000,
    retry: false,
    refetchInterval: pollMs ?? false,
  });
}
