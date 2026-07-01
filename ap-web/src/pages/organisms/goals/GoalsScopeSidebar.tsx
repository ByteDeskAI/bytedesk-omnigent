import { BotIcon, NetworkIcon } from "lucide-react";
import { EmptyScopeRow, ScopeAccordion, ScopeButton } from "@/components/goals";
import type { ScopeOption } from "@/components/goals/goals-utils";
import { Metric } from "@/components/shared";

export interface GoalsScopeSidebarProps {
  stats: { total: number; ready: number; waiting: number; blocked: number };
  organizationScope: ScopeOption;
  departmentScopes: ScopeOption[];
  employeeScopes: ScopeOption[];
  selectedScopeKey: string;
  setSelectedScopeKey: (key: string) => void;
  sectionsOpen: { department: boolean; employees: boolean };
  setSectionsOpen: React.Dispatch<
    React.SetStateAction<{ department: boolean; employees: boolean }>
  >;
}

export function GoalsScopeSidebar({
  stats,
  organizationScope,
  departmentScopes,
  employeeScopes,
  selectedScopeKey,
  setSelectedScopeKey,
  sectionsOpen,
  setSectionsOpen,
}: GoalsScopeSidebarProps) {
  return (
    <aside className="min-h-0 border-b border-border lg:border-r lg:border-b-0">
      <div className="flex h-full min-h-0 flex-col">
        <div className="grid grid-cols-4 gap-2 border-b border-border p-3">
          <Metric value={stats.total} label="Total" />
          <Metric value={stats.ready} label="Ready" />
          <Metric value={stats.waiting} label="Waiting" />
          <Metric value={stats.blocked} label="Blocked" />
        </div>
        <div className="shrink-0 border-b border-border px-3 py-2 text-xs font-medium text-muted-foreground">
          Scope
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-2">
          <ScopeButton
            scope={organizationScope}
            selected={organizationScope.key === selectedScopeKey}
            onSelect={() => setSelectedScopeKey(organizationScope.key)}
          />
          <ScopeAccordion
            label="Department"
            icon={<NetworkIcon className="size-4" />}
            open={sectionsOpen.department}
            onToggle={() =>
              setSectionsOpen((current) => ({
                ...current,
                department: !current.department,
              }))
            }
            count={departmentScopes.length}
          >
            {departmentScopes.map((scope) => (
              <ScopeButton
                key={scope.key}
                scope={scope}
                selected={scope.key === selectedScopeKey}
                onSelect={() => setSelectedScopeKey(scope.key)}
                nested
              />
            ))}
            {departmentScopes.length === 0 && <EmptyScopeRow label="No departments" />}
          </ScopeAccordion>
          <ScopeAccordion
            label="Employees"
            icon={<BotIcon className="size-4" />}
            open={sectionsOpen.employees}
            onToggle={() =>
              setSectionsOpen((current) => ({
                ...current,
                employees: !current.employees,
              }))
            }
            count={employeeScopes.length}
          >
            {employeeScopes.map((scope) => (
              <ScopeButton
                key={scope.key}
                scope={scope}
                selected={scope.key === selectedScopeKey}
                onSelect={() => setSelectedScopeKey(scope.key)}
                nested
              />
            ))}
            {employeeScopes.length === 0 && <EmptyScopeRow label="No employees" />}
          </ScopeAccordion>
        </div>
      </div>
    </aside>
  );
}