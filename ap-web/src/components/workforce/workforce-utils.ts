import {
  BotIcon,
  ShieldAlertIcon,
  TerminalIcon,
  WorkflowIcon,
} from "lucide-react";
import { createElement, type ReactNode } from "react";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { tierForAgent, type AgentTier } from "@/lib/agentTiers";
import type { ConnectorConnection, ConnectorManifest, ConnectorTool } from "@/lib/connectorsApi";
import type {
  WorkforceEffectiveConnector,
  WorkforceEffectiveSkill,
  WorkforceScopeKind,
} from "@/lib/workforceApi";

export type WorkForceTab =
  | "overview"
  | "config"
  | "permissions"
  | "skills"
  | "connectors"
  | "files";

export interface PendingSave {
  body: import("@/lib/agentImagesApi").AgentImageUpdate;
  label: string;
  tier: AgentTier;
}

export interface AvailableConnectorTool extends ConnectorTool {
  providerName: string;
  serviceKey: string;
  serviceName: string;
  token: string;
}

export interface DepartmentGroup {
  department: string;
  agents: AvailableAgent[];
}

export const ROSTER_SECTIONS_STORAGE_KEY = "workforce-roster-open-sections";

export function agentDisplayName(agent: AvailableAgent): string {
  return agent.display_name || agent.name;
}

export function compareText(a: string, b: string): number {
  return a.localeCompare(b, undefined, { sensitivity: "base" });
}

export function compareAgentsByName(a: AvailableAgent, b: AvailableAgent): number {
  return compareText(agentDisplayName(a), agentDisplayName(b)) || compareText(a.name, b.name);
}

export function departmentId(agent: AvailableAgent): string {
  return agent.department?.trim() || "Unassigned";
}

export function workforceScopeSlug(value: string | null | undefined): string | null {
  const cleaned = value
    ?.trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned || null;
}

export function isWorkForceEmployee(agent: AvailableAgent): boolean {
  return tierForAgent(agent) === "employee" && Boolean(agent.department?.trim());
}

export function workForceRosterAgents(agents: readonly AvailableAgent[]): AvailableAgent[] {
  return agents.filter((agent) => {
    const tier = tierForAgent(agent);
    return (
      tier === "system" || tier === "harness" || tier === "workflow" || isWorkForceEmployee(agent)
    );
  });
}

export function groupEmployeesByDepartment(agents: readonly AvailableAgent[]): DepartmentGroup[] {
  const groups = new Map<string, AvailableAgent[]>();
  for (const agent of agents) {
    if (!isWorkForceEmployee(agent)) continue;
    const department = departmentId(agent);
    groups.set(department, [...(groups.get(department) ?? []), agent]);
  }
  return Array.from(groups, ([department, departmentAgents]) => ({
    department,
    agents: [...departmentAgents].sort(compareAgentsByName),
  })).sort((a, b) => compareText(a.department, b.department));
}

export function tierLabel(tier: AgentTier): string {
  if (tier === "system") return "System Agents";
  if (tier === "harness") return "Harnesses";
  if (tier === "workflow") return "Workflows";
  return "Employees";
}

export function iconForTier(tier: AgentTier): ReactNode {
  if (tier === "system") return createElement(ShieldAlertIcon, { className: "size-4" });
  if (tier === "harness") return createElement(TerminalIcon, { className: "size-4" });
  if (tier === "workflow") return createElement(WorkflowIcon, { className: "size-4" });
  return createElement(BotIcon, { className: "size-4" });
}

export function tierAccentTextClass(tier: AgentTier): string {
  if (tier === "system") return "text-accent-purple";
  if (tier === "harness") return "text-accent-cyan";
  if (tier === "workflow") return "text-accent-amber";
  return "text-accent-blue";
}

export function tierAccentRingClass(tier: AgentTier): string {
  if (tier === "system") return "ring-accent-purple/40";
  if (tier === "harness") return "ring-accent-cyan/40";
  if (tier === "workflow") return "ring-accent-amber/40";
  return "ring-accent-blue/40";
}

export function tierAccentBorderClass(tier: AgentTier): string {
  if (tier === "system") return "border-accent-purple/40";
  if (tier === "harness") return "border-accent-cyan/40";
  if (tier === "workflow") return "border-accent-amber/40";
  return "border-accent-blue/40";
}

export function tierInitials(agent: AvailableAgent): string {
  const name = agentDisplayName(agent).trim();
  if (!name) return "?";
  const parts = name.split(/\s+/).filter(Boolean);
  if (parts.length === 1) return parts[0]!.slice(0, 2).toUpperCase();
  return `${parts[0]![0]}${parts[parts.length - 1]![0]}`.toUpperCase();
}

export function tierRequiresEditConfirmation(tier: AgentTier): boolean {
  return tier === "system" || tier === "harness";
}

export function editConfirmationTitle(tier: AgentTier | undefined): string {
  return tier === "harness" ? "Confirm harness edit" : "Confirm system agent edit";
}

export function editConfirmationDescription(tier: AgentTier | undefined): string {
  const label = tier === "harness" ? "harness" : "system agent";
  return `This change updates a ${label} image and becomes live for new sessions.`;
}

export function editConfirmationButtonLabel(tier: AgentTier | undefined): string {
  return tier === "harness" ? "Save harness" : "Save system agent";
}

export function parentPath(path: string): string {
  if (!path || path === ".") return "";
  const parts = path.split("/");
  parts.pop();
  return parts.join("/");
}

export function loadOpenRosterSections(): string[] {
  try {
    const raw = localStorage.getItem(ROSTER_SECTIONS_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

export function saveOpenRosterSections(sections: string[]): void {
  try {
    localStorage.setItem(ROSTER_SECTIONS_STORAGE_KEY, JSON.stringify(sections));
  } catch {
    // quota / private-mode — state still updates in memory, just won't persist
  }
}

export function toolsForConnection(
  provider: ConnectorManifest,
  connection: ConnectorConnection,
): AvailableConnectorTool[] {
  const serviceDefs = new Map(provider.services.map((service) => [service.key, service]));
  return connection.services.flatMap((state) => {
    if (!state.enabled) return [];
    const service = serviceDefs.get(state.serviceKey);
    if (!service) return [];
    return service.tools.map<AvailableConnectorTool>((tool) => ({
      ...tool,
      providerName: provider.name,
      serviceKey: state.serviceKey,
      serviceName: service.name,
      token: `${state.serviceKey}:${tool.key}`,
    }));
  });
}

export function scopeDisplayName(scopeKind: WorkforceScopeKind, department: string | null): string {
  return scopeKind === "organization" ? "Organization" : department || "Department";
}

export function inheritedSourceLabel(
  item: WorkforceEffectiveConnector | WorkforceEffectiveSkill,
): string {
  return item.inheritedFrom
    .map((source) => (source.scopeKind === "organization" ? "Organization" : "Department"))
    .join(", ");
}

export function sameConnectionSelection(
  a: Record<string, string[]>,
  b: Record<string, string[]>,
): boolean {
  const aKeys = Object.keys(a);
  const bKeys = Object.keys(b);
  if (aKeys.length !== bKeys.length) return false;
  return aKeys.every((key) => {
    const aValues = a[key] ?? [];
    const bValues = b[key] ?? [];
    return (
      aValues.length === bValues.length && aValues.every((value, index) => value === bValues[index])
    );
  });
}