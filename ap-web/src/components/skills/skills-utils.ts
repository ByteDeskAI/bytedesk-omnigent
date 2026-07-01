import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { HostFilesystemEntry } from "@/hooks/useHostFilesystem";

export type SkillScopeKind = "organization" | "department" | "employee";

export interface SkillScope {
  kind: SkillScopeKind;
  id: string;
}

export interface DepartmentGroup {
  id: string;
  agents: AvailableAgent[];
}

export function departmentId(agent: AvailableAgent): string {
  return agent.department?.trim() || "Unassigned";
}

export function scopeMatchesAgent(scope: SkillScope, agent: AvailableAgent): boolean {
  if (scope.kind === "organization") return true;
  if (scope.kind === "department") return departmentId(agent) === scope.id;
  return agent.id === scope.id;
}

export function scopeLabel(scope: SkillScope, agents: AvailableAgent[]): string {
  if (scope.kind === "organization") return "Organizational";
  if (scope.kind === "department") return scope.id;
  return agents.find((agent) => agent.id === scope.id)?.display_name ?? "Employee";
}

/**
 * The dedicated skills concierge built-in. Seeded agents carry a generated
 * `ag_…` id, so the stable handle is `name` ("skills-concierge"); fall back to
 * id, then a loose display-name match (display_name can be null on built-ins).
 */
export function findConcierge(agents: AvailableAgent[]): AvailableAgent | null {
  return (
    agents.find((a) => a.name === "skills-concierge") ??
    agents.find((a) => a.id === "skills-concierge") ??
    agents.find((a) => /skills.?concierge/i.test(a.display_name ?? "")) ??
    null
  );
}

export function homeWorkspaceFromEntries(entries: HostFilesystemEntry[]): string | null {
  const first = entries[0];
  if (!first) return null;
  const slash = first.path.lastIndexOf("/");
  if (slash < 0) return null;
  return slash === 0 ? "/" : first.path.slice(0, slash);
}