import { BotIcon, NetworkIcon, TargetIcon, Trash2Icon, XIcon } from "lucide-react";
import {
  EmptyScopeRow,
  GoalDetail,
  GoalRow,
  PlannerPanel,
  ScopeAccordion,
  ScopeButton,
  TemplatesPanel,
} from "@/components/goals";
import type { GoalView, ScopeOption } from "@/components/goals/goals-utils";
import { Metric, ViewButton } from "@/components/shared";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Link } from "@/lib/routing";
import type {
  GoalDependencyStatus,
  GoalPlannerSource,
  GoalRecord,
  GoalTemplate,
} from "@/lib/goalsApi";

export interface GoalsPageShellProps {
  selectedScope: ScopeOption;
  organizationScope: ScopeOption;
  departmentScopes: ScopeOption[];
  employeeScopes: ScopeOption[];
  selectedScopeKey: string;
  setSelectedScopeKey: (key: string) => void;
  sectionsOpen: { department: boolean; employees: boolean };
  setSectionsOpen: React.Dispatch<
    React.SetStateAction<{ department: boolean; employees: boolean }>
  >;
  stats: { total: number; ready: number; waiting: number; blocked: number };
  view: GoalView;
  setView: (view: GoalView) => void;
  goalsFetching: boolean;
  filteredGoals: GoalRecord[];
  selectedGoalId: string | null;
  setSelectedGoalId: (id: string | null) => void;
  selectedGoal: GoalRecord | null;
  plannerSources: GoalPlannerSource[];
  startPlanningBusy: boolean;
  error: string | null;
  setError: (error: string | null) => void;
  onStartPlanning: (sourceIds: string[]) => Promise<void>;
  templates: GoalTemplate[];
  templatesLoading: boolean;
  templatesError: boolean;
  instantiateBusy: boolean;
  onInstantiate: (template: GoalTemplate) => void;
  busy: boolean;
  newDependency: string;
  setNewDependency: (value: string) => void;
  onActivate: () => void;
  onStatus: (status: GoalRecord["status"]) => void;
  onAddDependency: () => void;
  onDependencyStatus: (dependencyId: string, status: GoalDependencyStatus) => void;
  onDeleteRequest: () => void;
  pendingDelete: GoalRecord | null;
  setPendingDelete: (goal: GoalRecord | null) => void;
  deleteBusy: boolean;
  onConfirmDelete: () => void;
}

export function GoalsPageShell({
  selectedScope,
  organizationScope,
  departmentScopes,
  employeeScopes,
  selectedScopeKey,
  setSelectedScopeKey,
  sectionsOpen,
  setSectionsOpen,
  stats,
  view,
  setView,
  goalsFetching,
  filteredGoals,
  selectedGoalId,
  setSelectedGoalId,
  selectedGoal,
  plannerSources,
  startPlanningBusy,
  error,
  setError,
  onStartPlanning,
  templates,
  templatesLoading,
  templatesError,
  instantiateBusy,
  onInstantiate,
  busy,
  newDependency,
  setNewDependency,
  onActivate,
  onStatus,
  onAddDependency,
  onDependencyStatus,
  onDeleteRequest,
  pendingDelete,
  setPendingDelete,
  deleteBusy,
  onConfirmDelete,
}: GoalsPageShellProps) {
  return (
    <div className="fixed inset-3 z-50 flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-background shadow-2xl">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted">
            <TargetIcon className="size-4" />
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-base font-semibold">Goals</h1>
            <p className="truncate text-xs text-muted-foreground">
              {selectedScope?.label ?? "All scopes"}
            </p>
          </div>
        </div>
        <Button variant="ghost" size="icon" asChild aria-label="Close goals">
          <Link to="/">
            <XIcon />
          </Link>
        </Button>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[18rem_minmax(0,1fr)_24rem]">
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

        <main className="min-h-0 overflow-hidden border-b border-border lg:border-r lg:border-b-0">
          <div className="flex h-full min-h-0 flex-col">
            <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
              <div className="flex items-center gap-1">
                <ViewButton active={view === "active"} onClick={() => setView("active")}>
                  Active
                </ViewButton>
                <ViewButton active={view === "waiting"} onClick={() => setView("waiting")}>
                  Waiting
                </ViewButton>
                <ViewButton active={view === "done"} onClick={() => setView("done")}>
                  Done
                </ViewButton>
              </div>
              <div className="text-xs text-muted-foreground">
                {goalsFetching ? "Refreshing" : `${filteredGoals.length} shown`}
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-auto p-3">
              {filteredGoals.map((goal) => (
                <GoalRow
                  key={goal.id}
                  goal={goal}
                  selected={goal.id === selectedGoalId}
                  onSelect={() => setSelectedGoalId(goal.id)}
                />
              ))}
              {filteredGoals.length === 0 && (
                <div className="flex min-h-52 items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
                  No goals match this view.
                </div>
              )}
            </div>
          </div>
        </main>

        <section className="min-h-0 overflow-auto">
          <div className="space-y-4 p-3">
            <PlannerPanel
              scope={selectedScope}
              sources={plannerSources}
              busy={startPlanningBusy}
              error={error}
              onStart={async (sourceIds) => {
                setError(null);
                await onStartPlanning(sourceIds);
              }}
            />

            <TemplatesPanel
              templates={templates}
              isLoading={templatesLoading}
              isError={templatesError}
              busy={instantiateBusy}
              scopeLabel={selectedScope.label}
              onInstantiate={onInstantiate}
            />

            {selectedGoal ? (
              <GoalDetail
                goal={selectedGoal}
                busy={busy}
                newDependency={newDependency}
                setNewDependency={setNewDependency}
                onActivate={onActivate}
                onStatus={onStatus}
                onAddDependency={onAddDependency}
                onDependencyStatus={onDependencyStatus}
                onDelete={onDeleteRequest}
              />
            ) : (
              <div className="rounded-md border border-border p-4 text-sm text-muted-foreground">
                Select a goal to inspect status, dependencies, and activation.
              </div>
            )}
          </div>
        </section>
      </div>

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete goal</DialogTitle>
            <DialogDescription>
              Permanently delete “{pendingDelete?.title}” and its dependencies. This cannot be
              undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">Cancel</Button>
            </DialogClose>
            <Button variant="destructive" disabled={deleteBusy} onClick={() => void onConfirmDelete()}>
              <Trash2Icon /> Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}