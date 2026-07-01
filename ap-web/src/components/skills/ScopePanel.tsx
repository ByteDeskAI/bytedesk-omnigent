import { BotIcon, Building2Icon, NetworkIcon } from "lucide-react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Metric } from "@/components/shared";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { ScopeButton } from "./ScopeButton";
import type { DepartmentGroup, SkillScope } from "./skills-utils";

export function ScopePanel({
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