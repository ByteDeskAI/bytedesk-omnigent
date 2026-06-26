/**
 * Three-tier grouping for the agent surfaces (new-chat picker, schedules,
 * goals, skills). Tier comes from the API's `category` field; older servers
 * (and rows whose category couldn't be projected) fall back to the legacy
 * `workflow` boolean. Pure functions so every surface groups identically.
 */

export type AgentTier = "system" | "employee" | "workflow";

// Insertion order IS the section order surfaces render.
export const TIER_LABELS: Record<AgentTier, string> = {
  system: "System",
  employee: "Employees",
  workflow: "Workflows",
};

function isAgentTier(value: string | undefined | null): value is AgentTier {
  return value === "system" || value === "employee" || value === "workflow";
}

/**
 * The tier for one agent. Prefers the explicit `category` when it's a valid
 * tier; otherwise the legacy heuristic — `workflow === true` → "workflow",
 * else "employee". "system" can only come from an explicit category; the
 * heuristic never invents it.
 */
export function tierForAgent(agent: { category?: string; workflow?: boolean }): AgentTier {
  if (isAgentTier(agent.category)) return agent.category;
  return agent.workflow === true ? "workflow" : "employee";
}

/**
 * Bucket agents by tier, preserving input order within each bucket.
 */
export function groupAgentsByTier<T extends { category?: string; workflow?: boolean }>(
  agents: readonly T[],
): { system: T[]; employee: T[]; workflow: T[] } {
  const groups: { system: T[]; employee: T[]; workflow: T[] } = {
    system: [],
    employee: [],
    workflow: [],
  };
  for (const agent of agents) groups[tierForAgent(agent)].push(agent);
  return groups;
}
