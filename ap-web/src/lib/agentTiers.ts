/**
 * Tier grouping for the agent surfaces (new-chat picker, schedules,
 * goals, skills). Tier comes from the API's `category` field; older servers
 * (and rows whose category couldn't be projected) fall back to the legacy
 * `workflow` boolean. Pure functions so every surface groups identically.
 */

export type AgentTier = "system" | "harness" | "employee" | "workflow";

// Insertion order IS the section order surfaces render.
export const TIER_LABELS: Record<AgentTier, string> = {
  system: "System",
  harness: "Harnesses",
  employee: "Employees",
  workflow: "Workflows",
};

function isAgentTier(value: string | undefined | null): value is AgentTier {
  return value === "system" || value === "harness" || value === "employee" || value === "workflow";
}

/**
 * The tier for one agent. Prefers the explicit `category` when it's a valid
 * tier; otherwise the legacy heuristic — `workflow === true` → "workflow",
 * else "employee". "system" and "harness" can only come from an explicit
 * category; the heuristic never invents either.
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
): { system: T[]; harness: T[]; employee: T[]; workflow: T[] } {
  const groups: { system: T[]; harness: T[]; employee: T[]; workflow: T[] } = {
    system: [],
    harness: [],
    employee: [],
    workflow: [],
  };
  for (const agent of agents) groups[tierForAgent(agent)].push(agent);
  return groups;
}
