import { useEffect, useMemo, useState } from "react";
import {
  DEFAULT_SCOPE_KEY,
  goalMatchesScope,
  goalMatchesView,
  scopeOptionsForGoals,
  type GoalView,
  type ScopeOption,
} from "@/components/goals/goals-utils";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { tierForAgent } from "@/lib/agentTiers";
import {
  useActivateGoal,
  useAddGoalDependency,
  useDeleteGoal,
  useGoalEvents,
  useGoalPlannerSources,
  useGoals,
  useGoalTemplates,
  useInstantiateGoalTemplate,
  useStartGoalPlanningSession,
  useUpdateGoal,
  useUpdateGoalDependency,
} from "@/hooks/useGoals";
import { useNavigate } from "@/lib/routing";
import type { GoalRecord, GoalTemplate } from "@/lib/goalsApi";
import type { GoalsPageShellProps } from "./organisms/GoalsPageShell";

export function useGoalsPage(): GoalsPageShellProps {
  const navigate = useNavigate();
  const agents = useAvailableAgents();
  const goals = useGoals({ include_dependencies: true });
  const plannerSources = useGoalPlannerSources();
  const startPlanningSession = useStartGoalPlanningSession();
  const updateGoal = useUpdateGoal();
  const activateGoal = useActivateGoal();
  const addDependency = useAddGoalDependency();
  const updateDependency = useUpdateGoalDependency();
  const deleteGoal = useDeleteGoal();
  const templates = useGoalTemplates();
  const instantiateTemplate = useInstantiateGoalTemplate();
  useGoalEvents(true);

  const agentRows = useMemo(() => agents.data ?? [], [agents.data]);
  const employeeRows = useMemo(
    () => agentRows.filter((agent) => tierForAgent(agent) !== "workflow"),
    [agentRows],
  );
  const goalRows = useMemo(() => goals.data ?? [], [goals.data]);
  const scopeOptions = useMemo(
    () => scopeOptionsForGoals(employeeRows, goalRows),
    [employeeRows, goalRows],
  );
  const organizationScope =
    scopeOptions.find((scope) => scope.kind === "organization") ??
    ({
      key: DEFAULT_SCOPE_KEY,
      kind: "organization",
      id: "omnigent",
      label: "Organization",
      subtitle: "All Omnigent work",
      count: goalRows.filter(
        (goal) => goal.target_kind === "organization" && goal.target_id === "omnigent",
      ).length,
    } satisfies ScopeOption);
  const departmentScopes = useMemo(
    () => scopeOptions.filter((scope) => scope.kind === "department"),
    [scopeOptions],
  );
  const employeeScopes = useMemo(
    () => scopeOptions.filter((scope) => scope.kind === "agent"),
    [scopeOptions],
  );

  const [selectedScopeKey, setSelectedScopeKey] = useState(DEFAULT_SCOPE_KEY);
  const [view, setView] = useState<GoalView>("active");
  const [selectedGoalId, setSelectedGoalId] = useState<string | null>(null);
  const [sectionsOpen, setSectionsOpen] = useState({ department: true, employees: true });
  const [newDependency, setNewDependency] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<GoalRecord | null>(null);

  const selectedScope =
    scopeOptions.find((scope) => scope.key === selectedScopeKey) ?? organizationScope;
  const selectedGoal = selectedGoalId
    ? (goalRows.find((goal) => goal.id === selectedGoalId) ?? null)
    : null;

  const filteredGoals = useMemo(
    () =>
      goalRows
        .filter((goal) => (selectedScope ? goalMatchesScope(goal, selectedScope) : true))
        .filter((goal) => goalMatchesView(goal, view))
        .sort((a, b) => a.priority - b.priority || b.updated_at - a.updated_at),
    [goalRows, selectedScope, view],
  );

  const stats = useMemo(
    () => ({
      total: goalRows.length,
      ready: goalRows.filter((goal) => goal.activation_state === "ready" && goal.status !== "done")
        .length,
      waiting: goalRows.filter(
        (goal) => goal.activation_state !== "ready" && goal.status !== "done",
      ).length,
      blocked: goalRows.filter((goal) => goal.status === "blocked").length,
    }),
    [goalRows],
  );

  useEffect(() => {
    if (!scopeOptions.some((scope) => scope.key === selectedScopeKey)) {
      setSelectedScopeKey(DEFAULT_SCOPE_KEY);
    }
  }, [scopeOptions, selectedScopeKey]);

  useEffect(() => {
    if (selectedGoalId && !goalRows.some((goal) => goal.id === selectedGoalId)) {
      setSelectedGoalId(null);
    }
  }, [goalRows, selectedGoalId]);

  async function addGoalDependency(goal: GoalRecord) {
    const label = newDependency.trim();
    if (!label) return;
    setError(null);
    try {
      await addDependency.mutateAsync({
        goalId: goal.id,
        payload: { kind: "manual", label, status: "pending" },
      });
      setNewDependency("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to add dependency.");
    }
  }

  async function confirmDeleteGoal() {
    if (!pendingDelete) return;
    setError(null);
    try {
      await deleteGoal.mutateAsync(pendingDelete.id);
      if (selectedGoalId === pendingDelete.id) setSelectedGoalId(null);
      setPendingDelete(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to delete goal.");
    }
  }

  async function instantiate(template: GoalTemplate) {
    setError(null);
    try {
      const goal = await instantiateTemplate.mutateAsync({
        templateId: template.id,
        payload: {
          overrides: {
            target_kind: selectedScope.kind,
            target_id: selectedScope.id,
            target_label: selectedScope.label,
          },
        },
      });
      setSelectedGoalId(goal.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to instantiate template.");
    }
  }

  const busy =
    startPlanningSession.isPending ||
    updateGoal.isPending ||
    activateGoal.isPending ||
    addDependency.isPending ||
    updateDependency.isPending ||
    deleteGoal.isPending ||
    instantiateTemplate.isPending;

  return {
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
    goalsFetching: goals.isFetching,
    filteredGoals,
    selectedGoalId,
    setSelectedGoalId,
    selectedGoal,
    plannerSources: plannerSources.data ?? [],
    startPlanningBusy: startPlanningSession.isPending,
    error,
    setError,
    onStartPlanning: async (sourceIds) => {
      try {
        const session = await startPlanningSession.mutateAsync({
          target_kind: selectedScope.kind,
          target_id: selectedScope.id,
          target_label: selectedScope.label,
          source_ids: sourceIds,
        });
        navigate(session.web_path);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Unable to start planning session.");
      }
    },
    templates: templates.data ?? [],
    templatesLoading: templates.isLoading,
    templatesError: templates.isError,
    instantiateBusy: instantiateTemplate.isPending,
    onInstantiate: (template) => void instantiate(template),
    busy,
    newDependency,
    setNewDependency,
    onActivate: () => {
      if (selectedGoal) void activateGoal.mutateAsync(selectedGoal.id);
    },
    onStatus: (status) => {
      if (selectedGoal) void updateGoal.mutateAsync({ goalId: selectedGoal.id, payload: { status } });
    },
    onAddDependency: () => {
      if (selectedGoal) void addGoalDependency(selectedGoal);
    },
    onDependencyStatus: (dependencyId, status) => {
      if (selectedGoal) {
        void updateDependency.mutateAsync({
          goalId: selectedGoal.id,
          dependencyId,
          payload: { status },
        });
      }
    },
    onDeleteRequest: () => {
      if (selectedGoal) setPendingDelete(selectedGoal);
    },
    pendingDelete,
    setPendingDelete,
    deleteBusy: deleteGoal.isPending,
    onConfirmDelete: () => void confirmDeleteGoal(),
  };
}