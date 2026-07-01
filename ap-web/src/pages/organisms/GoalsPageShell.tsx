import type { GoalView, ScopeOption } from "@/components/goals/goals-utils";
import type {
  GoalDependencyStatus,
  GoalPlannerSource,
  GoalRecord,
  GoalTemplate,
} from "@/lib/goalsApi";
import {
  GoalsDeleteDialog,
  GoalsDetailPanel,
  GoalsListMain,
  GoalsPageHeader,
  GoalsScopeSidebar,
} from "./goals";

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
      <GoalsPageHeader scopeLabel={selectedScope?.label ?? "All scopes"} />

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[18rem_minmax(0,1fr)_24rem]">
        <GoalsScopeSidebar
          stats={stats}
          organizationScope={organizationScope}
          departmentScopes={departmentScopes}
          employeeScopes={employeeScopes}
          selectedScopeKey={selectedScopeKey}
          setSelectedScopeKey={setSelectedScopeKey}
          sectionsOpen={sectionsOpen}
          setSectionsOpen={setSectionsOpen}
        />

        <GoalsListMain
          view={view}
          setView={setView}
          goalsFetching={goalsFetching}
          filteredGoals={filteredGoals}
          selectedGoalId={selectedGoalId}
          setSelectedGoalId={setSelectedGoalId}
        />

        <GoalsDetailPanel
          selectedScope={selectedScope}
          selectedGoal={selectedGoal}
          plannerSources={plannerSources}
          startPlanningBusy={startPlanningBusy}
          error={error}
          setError={setError}
          onStartPlanning={onStartPlanning}
          templates={templates}
          templatesLoading={templatesLoading}
          templatesError={templatesError}
          instantiateBusy={instantiateBusy}
          onInstantiate={onInstantiate}
          busy={busy}
          newDependency={newDependency}
          setNewDependency={setNewDependency}
          onActivate={onActivate}
          onStatus={onStatus}
          onAddDependency={onAddDependency}
          onDependencyStatus={onDependencyStatus}
          onDeleteRequest={onDeleteRequest}
        />
      </div>

      <GoalsDeleteDialog
        pendingDelete={pendingDelete}
        setPendingDelete={setPendingDelete}
        deleteBusy={deleteBusy}
        onConfirmDelete={onConfirmDelete}
      />
    </div>
  );
}