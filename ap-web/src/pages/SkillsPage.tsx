import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  BotIcon,
  Building2Icon,
  NetworkIcon,
  PuzzleIcon,
  XIcon,
} from "lucide-react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useInstalledSkills } from "@/hooks/useSkills";
import { AgentConversation, AgentComposer } from "@/components/chat";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";

type SkillScopeKind = "organization" | "department" | "employee";

interface SkillScope {
  kind: SkillScopeKind;
  id: string;
}

interface DepartmentGroup {
  id: string;
  agents: AvailableAgent[];
}

function departmentId(agent: AvailableAgent): string {
  return agent.department?.trim() || "Unassigned";
}

function scopeMatchesAgent(scope: SkillScope, agent: AvailableAgent): boolean {
  if (scope.kind === "organization") return true;
  if (scope.kind === "department") return departmentId(agent) === scope.id;
  return agent.id === scope.id;
}

function scopeLabel(scope: SkillScope, agents: AvailableAgent[]): string {
  if (scope.kind === "organization") return "Organizational";
  if (scope.kind === "department") return scope.id;
  return agents.find((agent) => agent.id === scope.id)?.display_name ?? "Employee";
}

/**
 * The dedicated skills concierge built-in. Seeded agents carry a generated
 * `ag_…` id, so the stable handle is `name` ("skills-concierge"); fall back to
 * id, then a loose display-name match (display_name can be null on built-ins).
 */
function findConcierge(agents: AvailableAgent[]): AvailableAgent | null {
  return (
    agents.find((a) => a.name === "skills-concierge") ??
    agents.find((a) => a.id === "skills-concierge") ??
    agents.find((a) => /skills.?concierge/i.test(a.display_name ?? "")) ??
    null
  );
}

export function SkillsPage() {
  const agentsQuery = useAvailableAgents();
  const installed = useInstalledSkills();
  const agents = useMemo(() => agentsQuery.data ?? [], [agentsQuery.data]);

  const [selectedScope, setSelectedScope] = useState<SkillScope>({
    kind: "organization",
    id: "omnigent",
  });
  const hasDefaultedScope = useRef(false);

  // Employee rows, ordered by department then display name — so the Employee
  // list (which maps agentRows) and every department group read in the same
  // department → name order. Case-insensitive so "Operations"/"engineering"
  // sort naturally rather than by ASCII case.
  const agentRows = useMemo(
    () =>
      agents
        .filter((agent) => agent.workflow !== true && Boolean(agent.department || agent.title))
        .sort(
          (a, b) =>
            departmentId(a).localeCompare(departmentId(b), undefined, { sensitivity: "base" }) ||
            a.display_name.localeCompare(b.display_name, undefined, { sensitivity: "base" }),
        ),
    [agents],
  );

  const departmentGroups = useMemo<DepartmentGroup[]>(() => {
    const groups = new Map<string, AvailableAgent[]>();
    for (const agent of agentRows) {
      const department = departmentId(agent);
      groups.set(department, [...(groups.get(department) ?? []), agent]);
    }
    // agentRows is already department→name ordered, so each group's agents keep
    // that order; sort the departments themselves by name (case-insensitive).
    return [...groups.entries()]
      .map(([id, departmentAgents]) => ({ id, agents: departmentAgents }))
      .sort((a, b) => a.id.localeCompare(b.id, undefined, { sensitivity: "base" }));
  }, [agentRows]);

  const targetAgentIds = useMemo(
    () =>
      agentRows.filter((agent) => scopeMatchesAgent(selectedScope, agent)).map((agent) => agent.id),
    [agentRows, selectedScope],
  );

  useEffect(() => {
    if (!hasDefaultedScope.current && agentRows.length > 0) {
      setSelectedScope({ kind: "organization", id: "omnigent" });
      hasDefaultedScope.current = true;
      return;
    }
    if (
      agentRows.length > 0 &&
      selectedScope.kind !== "organization" &&
      !agentRows.some((agent) => scopeMatchesAgent(selectedScope, agent))
    ) {
      setSelectedScope({ kind: "organization", id: "omnigent" });
    }
  }, [agentRows, selectedScope]);

  const selectedScopeLabel = scopeLabel(selectedScope, agentRows);
  const concierge = useMemo(() => findConcierge(agents), [agents]);

  const scopedInstalled = useMemo(
    () =>
      (installed.data ?? [])
        .map((skill) => ({
          ...skill,
          agents: skill.agents.filter((agent) => targetAgentIds.includes(agent.id)),
        }))
        .filter((skill) => skill.agents.length > 0),
    [installed.data, targetAgentIds],
  );

  return (
    <div className="fixed inset-3 z-50 flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-background shadow-2xl">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted">
            <PuzzleIcon className="size-4" />
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-base font-semibold">Skills</h1>
            <p className="truncate text-xs text-muted-foreground">{selectedScopeLabel}</p>
          </div>
        </div>
        <Button variant="ghost" size="icon" asChild aria-label="Close skills">
          <Link to="/">
            <XIcon />
          </Link>
        </Button>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[18rem_minmax(0,1fr)_22rem]">
        <ScopePanel
          selectedScope={selectedScope}
          setSelectedScope={setSelectedScope}
          agentRows={agentRows}
          departmentGroups={departmentGroups}
          targetCount={targetAgentIds.length}
        />

        <main className="flex min-h-0 flex-col overflow-hidden border-b border-border lg:border-r lg:border-b-0">
          {concierge ? (
            <>
              <AgentConversation />
              <AgentComposer
                agentId={concierge.id}
                agents={agents}
                agentsLoading={agentsQuery.isLoading}
              />
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center px-6 text-center">
              <p className="max-w-sm text-sm text-muted-foreground">
                Skills assistant isn't available yet.
              </p>
            </div>
          )}
        </main>

        <aside className="min-h-0 overflow-auto">
          <div className="space-y-4 p-3">
            <section className="rounded-md border border-border bg-background">
              <div className="flex items-center justify-between border-b border-border px-3 py-2">
                <h2 className="text-sm font-medium">Available Skills</h2>
                <span className="text-xs text-muted-foreground">{scopedInstalled.length}</span>
              </div>
              <div className="max-h-[30rem] divide-y divide-border overflow-y-auto">
                {scopedInstalled.map((skill) => (
                  <div key={skill.name} className="px-3 py-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{skill.name}</div>
                      <div className="line-clamp-2 text-xs text-muted-foreground">
                        {skill.description}
                      </div>
                    </div>
                    <div className="mt-2 text-xs text-muted-foreground">
                      {skill.agents.length} in scope
                    </div>
                  </div>
                ))}
                {!installed.isLoading && scopedInstalled.length === 0 && (
                  <div className="px-3 py-6 text-sm text-muted-foreground">
                    No skills available for this scope.
                  </div>
                )}
              </div>
            </section>
          </div>
        </aside>
      </div>
    </div>
  );
}

function ScopePanel({
  selectedScope,
  setSelectedScope,
  agentRows,
  departmentGroups,
  targetCount,
}: {
  selectedScope: SkillScope;
  setSelectedScope: (scope: SkillScope) => void;
  agentRows: AvailableAgent[];
  departmentGroups: DepartmentGroup[];
  targetCount: number;
}) {
  return (
    <aside className="min-h-0 border-b border-border lg:border-r lg:border-b-0">
      <div className="flex h-full min-h-0 flex-col">
        <div className="grid grid-cols-2 gap-2 border-b border-border p-3">
          <Metric value={agentRows.length} label="Employees" />
          <Metric value={targetCount} label="Targeted" />
        </div>
        <div className="shrink-0 border-b border-border px-3 py-2 text-xs font-medium text-muted-foreground">
          Scope
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-2">
          <ScopeButton
            icon={<Building2Icon className="size-4" />}
            label="Organizational"
            subtitle="All employee agents"
            count={agentRows.length}
            selected={selectedScope.kind === "organization"}
            onClick={() => setSelectedScope({ kind: "organization", id: "omnigent" })}
          />

          <Accordion type="multiple" defaultValue={["departments", "employees"]}>
            <AccordionItem value="departments" className="border-0">
              <AccordionTrigger className="px-2 py-2 text-xs text-muted-foreground hover:no-underline">
                Departmental
              </AccordionTrigger>
              <AccordionContent className="pb-1">
                {departmentGroups.map((department) => (
                  <ScopeButton
                    key={department.id}
                    icon={<NetworkIcon className="size-4" />}
                    label={department.id}
                    subtitle="Department"
                    count={department.agents.length}
                    selected={
                      selectedScope.kind === "department" && selectedScope.id === department.id
                    }
                    onClick={() => setSelectedScope({ kind: "department", id: department.id })}
                  />
                ))}
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="employees" className="border-0">
              <AccordionTrigger className="px-2 py-2 text-xs text-muted-foreground hover:no-underline">
                Employee
              </AccordionTrigger>
              <AccordionContent className="pb-1">
                {agentRows.map((agent) => (
                  <ScopeButton
                    key={agent.id}
                    icon={<BotIcon className="size-4" />}
                    label={agent.display_name}
                    subtitle={agent.title || agent.name}
                    count={undefined}
                    selected={selectedScope.kind === "employee" && selectedScope.id === agent.id}
                    onClick={() => setSelectedScope({ kind: "employee", id: agent.id })}
                  />
                ))}
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </div>
      </div>
    </aside>
  );
}

function ScopeButton({
  icon,
  label,
  subtitle,
  count,
  selected,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  subtitle: string;
  count: number | undefined;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "mb-1 flex min-h-12 w-full cursor-pointer items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50",
        selected
          ? "border-border bg-muted text-foreground"
          : "border-transparent text-muted-foreground hover:bg-muted/60 hover:text-foreground",
      )}
      aria-pressed={selected}
    >
      <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-background">
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{label}</span>
        <span className="block truncate text-xs text-muted-foreground">{subtitle}</span>
      </span>
      {count !== undefined && <Badge variant="secondary">{count}</Badge>}
    </button>
  );
}

function Metric({ value, label }: { value: number; label: string }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-muted/30 px-2 py-1.5">
      <div className="truncate text-sm font-semibold tabular-nums">{value}</div>
      <div className="truncate text-[0.68rem] text-muted-foreground">{label}</div>
    </div>
  );
}
