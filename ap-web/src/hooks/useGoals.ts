import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import {
  activateGoal,
  addGoalDependency,
  commitGoalPlanningSession,
  createGoal,
  createGoalTemplate,
  deleteGoal,
  deleteGoalTemplate,
  getGoal,
  instantiateGoalTemplate,
  listGoals,
  listGoalPlannerSources,
  listGoalTemplates,
  startGoalPlanningSession,
  updateGoal,
  updateGoalDependency,
  updateGoalTemplate,
  type CommitGoalPlanningSessionRequest,
  type CreateGoalDependencyRequest,
  type CreateGoalRequest,
  type CreateGoalTemplateRequest,
  type GoalFilters,
  type InstantiateGoalTemplateRequest,
  type StartGoalPlanningSessionRequest,
  type UpdateGoalDependencyRequest,
  type UpdateGoalRequest,
  type UpdateGoalTemplateRequest,
} from "@/lib/goalsApi";

export function useGoals(filters: GoalFilters = {}) {
  return useQuery({
    queryKey: ["goals", filters],
    queryFn: () => listGoals(filters),
    staleTime: 15_000,
  });
}

export function useGoal(goalId?: string) {
  return useQuery({
    queryKey: ["goal", goalId ?? "none"],
    queryFn: () => getGoal(goalId as string),
    enabled: Boolean(goalId),
    staleTime: 15_000,
  });
}

export function useCreateGoal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateGoalRequest) => createGoal(payload),
    onSuccess: (goal) => {
      void queryClient.invalidateQueries({ queryKey: ["goals"] });
      void queryClient.setQueryData(["goal", goal.id], goal);
    },
  });
}

export function useGoalPlannerSources() {
  return useQuery({
    queryKey: ["goal-planner-sources"],
    queryFn: listGoalPlannerSources,
    staleTime: 60_000,
  });
}

export function useStartGoalPlanningSession() {
  return useMutation({
    mutationFn: (payload: StartGoalPlanningSessionRequest) => startGoalPlanningSession(payload),
  });
}

export function useCommitGoalPlanningSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      sessionId,
      payload,
    }: {
      sessionId: string;
      payload: CommitGoalPlanningSessionRequest;
    }) => commitGoalPlanningSession(sessionId, payload),
    onSuccess: (goal) => {
      void queryClient.invalidateQueries({ queryKey: ["goals"] });
      void queryClient.setQueryData(["goal", goal.id], goal);
    },
  });
}

export function useUpdateGoal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ goalId, payload }: { goalId: string; payload: UpdateGoalRequest }) =>
      updateGoal(goalId, payload),
    onSuccess: (goal) => {
      void queryClient.invalidateQueries({ queryKey: ["goals"] });
      void queryClient.setQueryData(["goal", goal.id], goal);
    },
  });
}

export function useActivateGoal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: activateGoal,
    onSuccess: (goal) => {
      void queryClient.invalidateQueries({ queryKey: ["goals"] });
      void queryClient.setQueryData(["goal", goal.id], goal);
    },
  });
}

export function useAddGoalDependency() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      goalId,
      payload,
    }: {
      goalId: string;
      payload: CreateGoalDependencyRequest;
    }) => addGoalDependency(goalId, payload),
    onSuccess: (_dependency, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["goals"] });
      void queryClient.invalidateQueries({ queryKey: ["goal", variables.goalId] });
    },
  });
}

export function useDeleteGoal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (goalId: string) => deleteGoal(goalId),
    onSuccess: (_void, goalId) => {
      void queryClient.invalidateQueries({ queryKey: ["goals"] });
      queryClient.removeQueries({ queryKey: ["goal", goalId] });
    },
  });
}

export function useGoalTemplates() {
  return useQuery({
    queryKey: ["goal-templates"],
    queryFn: listGoalTemplates,
    staleTime: 60_000,
  });
}

export function useCreateGoalTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateGoalTemplateRequest) => createGoalTemplate(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["goal-templates"] });
    },
  });
}

export function useUpdateGoalTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      templateId,
      payload,
    }: {
      templateId: string;
      payload: UpdateGoalTemplateRequest;
    }) => updateGoalTemplate(templateId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["goal-templates"] });
    },
  });
}

export function useDeleteGoalTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (templateId: string) => deleteGoalTemplate(templateId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["goal-templates"] });
    },
  });
}

export function useInstantiateGoalTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      templateId,
      payload,
    }: {
      templateId: string;
      payload?: InstantiateGoalTemplateRequest;
    }) => instantiateGoalTemplate(templateId, payload),
    onSuccess: (goal) => {
      void queryClient.invalidateQueries({ queryKey: ["goals"] });
      void queryClient.setQueryData(["goal", goal.id], goal);
    },
  });
}

export function useUpdateGoalDependency() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      goalId,
      dependencyId,
      payload,
    }: {
      goalId: string;
      dependencyId: string;
      payload: UpdateGoalDependencyRequest;
    }) => updateGoalDependency(goalId, dependencyId, payload),
    onSuccess: (_dependency, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["goals"] });
      void queryClient.invalidateQueries({ queryKey: ["goal", variables.goalId] });
    },
  });
}

export function useGoalEvents(enabled = true) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();

    async function connect() {
      try {
        const res = await authenticatedFetch("/v1/goals/events", {
          signal: controller.signal,
        });
        if (!res.ok || !res.body) return;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        async function pump(): Promise<void> {
          const { value, done } = await reader.read();
          if (done || controller.signal.aborted) return;
          buffer += decoder.decode(value, { stream: true });
          const chunks = buffer.split("\n\n");
          buffer = chunks.pop() ?? "";
          for (const chunk of chunks) {
            const dataLine = chunk
              .split("\n")
              .find((line) => line.startsWith("data:"));
            if (!dataLine) continue;
            const event = JSON.parse(dataLine.slice(5)) as {
              type?: string;
              goalId?: string;
              entity?: string;
            };
            if (event.type === "goal.changed" || event.type === "goal.planning.committed") {
              void queryClient.invalidateQueries({ queryKey: ["goals"] });
              if (event.goalId) {
                void queryClient.invalidateQueries({ queryKey: ["goal", event.goalId] });
              }
            } else if (event.type === "goal.planning.started") {
              void queryClient.invalidateQueries({ queryKey: ["goal-planner-sources"] });
            } else if (event.type === "entity.changed") {
              // BDP-2588: condition/budget/template/delete deltas over the same stream.
              void queryClient.invalidateQueries({ queryKey: ["goals"] });
              if (event.entity === "template") {
                void queryClient.invalidateQueries({ queryKey: ["goal-templates"] });
              }
              if (event.goalId) {
                void queryClient.invalidateQueries({ queryKey: ["goal", event.goalId] });
              }
            }
          }

          await pump();
        }

        await pump();
      } catch (error) {
        if (!controller.signal.aborted) {
          console.warn("Goal event stream disconnected", error);
        }
      }
    }

    void connect();
    return () => {
      controller.abort();
    };
  }, [enabled, queryClient]);
}
