import {
  SearchIcon,
  ShieldAlertIcon,
  TerminalIcon,
  UsersIcon,
  WorkflowIcon,
} from "lucide-react";
import { useMemo, useRef, useState, type RefObject } from "react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { groupAgentsByTier, type AgentTier } from "@/lib/agentTiers";
import { cn } from "@/lib/utils";
import { RosterButton } from "./RosterButton";
import {
  compareAgentsByName,
  groupEmployeesByDepartment,
  loadOpenRosterSections,
  saveOpenRosterSections,
  tierAccentTextClass,
  tierLabel,
} from "./workforce-utils";

export function RosterPanel({
  agents,
  selectedAgentId,
  setSelectedAgentId,
  query,
  setQuery,
}: {
  agents: AvailableAgent[];
  selectedAgentId: string | null;
  setSelectedAgentId: (id: string) => void;
  query: string;
  setQuery: (query: string) => void;
}) {
  const groups = useMemo(() => groupAgentsByTier(agents), [agents]);
  const departmentGroups = useMemo(() => groupEmployeesByDepartment(agents), [agents]);
  const employeeCount = departmentGroups.reduce((count, group) => count + group.agents.length, 0);
  const systemAgents = useMemo(() => [...groups.system].sort(compareAgentsByName), [groups.system]);
  const harnessAgents = useMemo(
    () => [...groups.harness].sort(compareAgentsByName),
    [groups.harness],
  );
  const workflowAgents = useMemo(
    () => [...groups.workflow].sort(compareAgentsByName),
    [groups.workflow],
  );

  const [openSections, setOpenSections] = useState<string[]>(() => loadOpenRosterSections());
  function handleOpenSectionsChange(next: string[]) {
    setOpenSections(next);
    saveOpenRosterSections(next);
  }

  const employeesRef = useRef<HTMLElement>(null);
  const systemRef = useRef<HTMLElement>(null);
  const harnessRef = useRef<HTMLElement>(null);
  const workflowRef = useRef<HTMLElement>(null);
  const jumpTargets: Record<AgentTier, RefObject<HTMLElement | null>> = {
    employee: employeesRef,
    system: systemRef,
    harness: harnessRef,
    workflow: workflowRef,
  };
  function jumpTo(tier: AgentTier) {
    jumpTargets[tier].current?.scrollIntoView({ behavior: "smooth", block: "start" });
    const sectionId = `tier:${tier}`;
    if (tier !== "employee" && !openSections.includes(sectionId)) {
      handleOpenSectionsChange([...openSections, sectionId]);
    }
  }

  return (
    <aside
      aria-label="Agent roster"
      className="min-h-0 border-b border-border bg-background lg:border-r lg:border-b-0"
    >
      <div className="flex h-full min-h-0 flex-col">
        <header className="mc-surface m-2 mb-0 shrink-0 rounded-b-none border-b-0 px-4 py-4">
          <div className="flex items-center gap-2.5">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-md border border-accent-blue/40 bg-accent-blue/10 text-accent-blue shadow-[var(--shadow-glow-blue)]">
              <UsersIcon className="size-4" />
            </span>
            <div className="min-w-0">
              <h1 className="truncate text-base font-semibold">Work Force</h1>
              <p className="mc-label truncate text-accent-blue/70">Agent directory control</p>
            </div>
          </div>
          <div className="relative mt-3">
            <SearchIcon className="absolute top-2 left-2 size-4 text-muted-foreground" />
            <Input
              className="pl-8"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search agents"
              aria-label="Search agents"
            />
          </div>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {[
              {
                tier: "employee" as const,
                count: employeeCount,
                accent: "text-accent-blue",
                icon: <UsersIcon className="size-3.5" />,
              },
              {
                tier: "system" as const,
                count: systemAgents.length,
                accent: "text-accent-purple",
                icon: <ShieldAlertIcon className="size-3.5" />,
              },
              {
                tier: "harness" as const,
                count: harnessAgents.length,
                accent: "text-accent-cyan",
                icon: <TerminalIcon className="size-3.5" />,
              },
              {
                tier: "workflow" as const,
                count: workflowAgents.length,
                accent: "text-accent-amber",
                icon: <WorkflowIcon className="size-3.5" />,
              },
            ].map((chip) => (
              <button
                key={chip.tier}
                type="button"
                onClick={() => jumpTo(chip.tier)}
                aria-label={`${tierLabel(chip.tier)} — ${chip.count}`}
                title={tierLabel(chip.tier)}
                className="flex items-center gap-1 rounded-full border border-border-dimmer bg-bg-subtle px-2 py-1 transition-colors hover:border-border-stronger hover:bg-muted/50"
              >
                <span className={chip.accent}>{chip.icon}</span>
                <span className="mc-value text-2xs">{chip.count}</span>
              </button>
            ))}
          </div>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          <Accordion
            type="multiple"
            value={openSections}
            onValueChange={handleOpenSectionsChange}
            className="gap-1"
          >
            <section ref={employeesRef} className="mb-3 scroll-mt-2">
              <div className="mc-label mb-1 flex items-center justify-between px-2">
                <span>Employees</span>
                <span className="mc-value">{employeeCount}</span>
              </div>
              {departmentGroups.length > 0 ? (
                departmentGroups.map((group) => (
                  <AccordionItem
                    key={group.department}
                    value={`department:${group.department}`}
                    className="border-0"
                  >
                    <AccordionTrigger
                      aria-label={`Department ${group.department}`}
                      className="rounded-md px-2 py-2 text-xs text-muted-foreground hover:bg-muted/40 hover:no-underline"
                    >
                      <span className="flex flex-1 items-center justify-between pr-2">
                        <span>{group.department}</span>
                        <Badge variant="secondary">{group.agents.length}</Badge>
                      </span>
                    </AccordionTrigger>
                    <AccordionContent className="space-y-1 pb-1">
                      {group.agents.map((agent) => (
                        <RosterButton
                          key={agent.id}
                          agent={agent}
                          selected={selectedAgentId === agent.id}
                          onSelect={() => setSelectedAgentId(agent.id)}
                        />
                      ))}
                    </AccordionContent>
                  </AccordionItem>
                ))
              ) : (
                <div className="px-2 py-3 text-xs text-muted-foreground">No employees.</div>
              )}
            </section>

            {(
              [
                { tier: "system" as const, agents: systemAgents, ref: systemRef },
                { tier: "harness" as const, agents: harnessAgents, ref: harnessRef },
                { tier: "workflow" as const, agents: workflowAgents, ref: workflowRef },
              ] satisfies { tier: AgentTier; agents: AvailableAgent[]; ref: typeof systemRef }[]
            ).map((section) => (
              <section key={section.tier} ref={section.ref} className="mb-3 scroll-mt-2">
                <AccordionItem value={`tier:${section.tier}`} className="border-0">
                  <AccordionTrigger
                    aria-label={tierLabel(section.tier)}
                    className="rounded-md px-2 py-2 text-xs text-muted-foreground hover:bg-muted/40 hover:no-underline"
                  >
                    <span className="flex flex-1 items-center justify-between pr-2">
                      <span className={cn("mc-label", tierAccentTextClass(section.tier))}>
                        {tierLabel(section.tier)}
                      </span>
                      <span className="mc-value">{section.agents.length}</span>
                    </span>
                  </AccordionTrigger>
                  <AccordionContent className="space-y-1 pb-1">
                    {section.agents.length > 0 ? (
                      section.agents.map((agent) => (
                        <RosterButton
                          key={agent.id}
                          agent={agent}
                          selected={selectedAgentId === agent.id}
                          onSelect={() => setSelectedAgentId(agent.id)}
                        />
                      ))
                    ) : (
                      <div className="px-2 py-3 text-xs text-muted-foreground">No agents.</div>
                    )}
                  </AccordionContent>
                </AccordionItem>
              </section>
            ))}
          </Accordion>
        </div>
      </div>
    </aside>
  );
}