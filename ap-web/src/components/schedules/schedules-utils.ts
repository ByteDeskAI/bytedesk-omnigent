import type { TaskTemplate } from "@/lib/schedulesApi";

export type CalendarView = "week" | "day";
export type SurfaceMode = "calendar" | "create";
export type WorkflowMode = "existing" | "new";

export const dayFormatter = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
});
export const timeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
});
export const monthFormatter = new Intl.DateTimeFormat(undefined, {
  month: "long",
  day: "numeric",
  year: "numeric",
});

export function startOfDay(date: Date): Date {
  const next = new Date(date);
  next.setHours(0, 0, 0, 0);
  return next;
}

export function startOfWeek(date: Date): Date {
  const next = startOfDay(date);
  const day = next.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  next.setDate(next.getDate() + diff);
  return next;
}

export function addDays(date: Date, amount: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + amount);
  return next;
}

export function addHours(date: Date, hour: number): Date {
  const next = startOfDay(date);
  next.setHours(hour, 0, 0, 0);
  return next;
}

export function dayKey(epochSeconds: number): string {
  return startOfDay(new Date(epochSeconds * 1000)).toISOString();
}

export function occurrenceHour(epochSeconds: number): number {
  return new Date(epochSeconds * 1000).getHours();
}

export function taskPrompt(task: TaskTemplate | undefined): string {
  const prompt = task?.payload?.prompt;
  return typeof prompt === "string" ? prompt : "";
}