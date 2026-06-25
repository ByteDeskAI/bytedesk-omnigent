import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createSchedule,
  createTask,
  draftCadence,
  listScheduleOccurrences,
  listSchedules,
  listTasks,
  type CreateScheduleRequest,
  type CreateTaskRequest,
} from "@/lib/schedulesApi";

export function useTaskTemplates() {
  return useQuery({
    queryKey: ["task-templates"],
    queryFn: listTasks,
    staleTime: 30_000,
  });
}

export function useSchedules(agentId?: string) {
  return useQuery({
    queryKey: ["schedules", agentId ?? "all"],
    queryFn: () => listSchedules(agentId),
    enabled: Boolean(agentId),
    staleTime: 15_000,
  });
}

export function useScheduleOccurrences(agentId: string | undefined, start: string, end: string) {
  return useQuery({
    queryKey: ["schedule-occurrences", agentId ?? "none", start, end],
    queryFn: () => listScheduleOccurrences({ agentId, start, end }),
    enabled: Boolean(agentId),
    staleTime: 15_000,
  });
}

export function useCreateTaskTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateTaskRequest) => createTask(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["task-templates"] });
    },
  });
}

export function useCreateSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateScheduleRequest) => createSchedule(payload),
    onSuccess: (_schedule, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["schedules", variables.agent_id] });
      void queryClient.invalidateQueries({
        queryKey: ["schedule-occurrences", variables.agent_id],
      });
    },
  });
}

export function useDraftCadence() {
  return useMutation({
    mutationFn: draftCadence,
  });
}
