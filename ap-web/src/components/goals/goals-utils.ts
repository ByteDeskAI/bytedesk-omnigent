import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type {
  GoalActivationState,
  GoalReadinessKind,
  GoalRecord,
  GoalStatus,
  GoalTargetKind,
} from "@/lib/goalsApi";

export type ScopeKind = GoalTargetKind;
export type GoalView = "active" | "waiting" | "done";

export interface ScopeOption {
  key: string;
  kind: ScopeKind;
  id: string;
  label: string;
  subtitle: string;
  count: number;
}

export const STATUS_OPTIONS: GoalStatus[] = [
  "open",
  "assigned",
  "in_progress",
  "blocked",
  "done",
];

export function scopeKey(kind: ScopeKind, id: string) {
  return `${kind}:${id}`;
}

export const DEFAULT_SCOPE_KEY = scopeKey("organization", "omnigent");

export function departmentId(agent: AvailableAgent): string {
  return agent.department?.trim() || "Unassigned";
}

export function displayTarget(goal: GoalRecord): string {
  return goal.target_label || goal.target_id;
}

export function statusLabel(status: GoalStatus): string {
  if (status === "in_progress") return "In progress";
  return status.charAt(0).toUpperCase() + status.slice(1);
}

export function readinessLabel(readiness: GoalReadinessKind): string {
  return readiness.charAt(0).toUpperCase() + readiness.slice(1);
}

export function activationLabel(activation: GoalActivationState): string {
  return activation.charAt(0).toUpperCase() + activation.slice(1);
}

export function formattedTime(epochSeconds: number): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(epochSeconds * 1000));
}

export function goalMatchesScope(goal: GoalRecord, scope: ScopeOption): boolean {
  return goal.target_kind === scope.kind && goal.target_id === scope.id;
}

export function goalMatchesView(goal: GoalRecord, view: GoalView): boolean {
  if (view === "done") return goal.status === "done";
  if (view === "waiting") return goal.status !== "done" && goal.activation_state !== "ready";
  return goal.status !== "done" && goal.activation_state === "ready";
}

export function pendingDependencyCount(goal: GoalRecord): number {
  return goal.dependencies.filter((dependency) => dependency.status === "pending").length;
}

export function scopeOptionsForGoals(
  agents: AvailableAgent[],
  goals: GoalRecord[],
): ScopeOption[] {
  const departments = Array.from(new Set(agents.map(departmentId))).sort((a, b) =>
    a.localeCompare(b),
  );
  const targets = [
    {
      key: scopeKey("organization", "omnigent"),
      kind: "organization" as const,
      id: "omnigent",
      label: "Organization",
      subtitle: "All Omnigent work",
    },
    ...departments.map((department) => ({
      key: scopeKey("department", department),
      kind: "department" as const,
      id: department,
      label: department,
      subtitle: "Department goal",
    })),
    ...agents.map((agent) => ({
      key: scopeKey("agent", agent.id),
      kind: "agent" as const,
      id: agent.id,
      label: agent.display_name,
      subtitle: agent.title || agent.name,
    })),
  ];
  return targets.map((target) => ({
    key: scopeKey(target.kind, target.id),
    kind: target.kind,
    id: target.id,
    label: target.label,
    subtitle: target.subtitle,
    count: goals.filter(
      (goal) => goal.target_kind === target.kind && goal.target_id === target.id,
    ).length,
  }));
}

// ADR-0154: delivery milestones live in goal.payload.hierarchy.milestones, each
// gated by the two-key rule (Jira Task Done AND PR merged). awaiting_pr /
// awaiting_jira surface which key is still missing.
export interface GoalMilestone {
  taskKey?: string;
  title?: string;
  status?: string;
  steps?: string[];
  stepsDone?: string[];
}

export function goalMilestones(goal: GoalRecord): GoalMilestone[] {
  const hierarchy = (goal.payload as { hierarchy?: { milestones?: unknown } } | null)
    ?.hierarchy;
  const milestones = hierarchy?.milestones;
  return Array.isArray(milestones) ? (milestones as GoalMilestone[]) : [];
}

export const MILESTONE_LABELS: Record<string, string> = {
  pending: "Pending",
  in_progress: "In progress",
  awaiting_pr: "Awaiting PR",
  awaiting_jira: "Awaiting Jira",
  done: "Done",
};

export function milestoneLabel(status?: string): string {
  return MILESTONE_LABELS[status ?? "pending"] ?? "Pending";
}