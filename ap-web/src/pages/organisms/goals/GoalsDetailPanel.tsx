import { GoalDetail, PlannerPanel, TemplatesPanel } from "@/components/goals";
import type { ScopeOption } from "@/components/goals/goals-utils";
import type {
  GoalDependencyStatus,
  GoalPlannerSource,
  GoalRecord,
  GoalTemplate,
} from "@/lib/goalsApi";

export interface GoalsDetailPanelProps {
  selectedScope: ScopeOption;
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
}

export function GoalsDetailPanel({
  selectedScope,
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
}: GoalsDetailPanelProps) {
  return (
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
  );
}