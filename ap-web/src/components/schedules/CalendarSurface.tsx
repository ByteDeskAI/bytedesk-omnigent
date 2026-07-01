import { useMemo } from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ScheduleOccurrence } from "@/lib/schedulesApi";
import { DayView } from "./DayView";
import {
  addDays,
  dayFormatter,
  dayKey,
  monthFormatter,
  startOfDay,
  startOfWeek,
  timeFormatter,
  type CalendarView,
} from "./schedules-utils";

export function CalendarSurface({
  view,
  setView,
  calendarDate,
  setCalendarDate,
  occurrences,
  schedulesCount,
  loading,
  onCreate,
}: {
  view: CalendarView;
  setView: (value: CalendarView) => void;
  calendarDate: Date;
  setCalendarDate: (value: Date) => void;
  occurrences: ScheduleOccurrence[];
  schedulesCount: number;
  loading: boolean;
  onCreate: (slot: Date) => void;
}) {
  const weekStart = startOfWeek(calendarDate);
  const days = Array.from({ length: 7 }, (_, index) => addDays(weekStart, index));
  const occurrencesByDay = useMemo(() => {
    const map = new Map<string, ScheduleOccurrence[]>();
    for (const occurrence of occurrences) {
      const key = dayKey(occurrence.fire_at);
      const next = map.get(key) ?? [];
      next.push(occurrence);
      map.set(key, next);
    }
    return map;
  }, [occurrences]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="icon"
            aria-label="Previous period"
            onClick={() => setCalendarDate(addDays(calendarDate, view === "week" ? -7 : -1))}
          >
            <ChevronLeftIcon />
          </Button>
          <div className="min-w-40 text-sm font-medium">{monthFormatter.format(calendarDate)}</div>
          <Button
            variant="ghost"
            size="icon"
            aria-label="Next period"
            onClick={() => setCalendarDate(addDays(calendarDate, view === "week" ? 7 : 1))}
          >
            <ChevronRightIcon />
          </Button>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline">{schedulesCount} active</Badge>
          <Button
            variant={view === "week" ? "default" : "outline"}
            size="sm"
            onClick={() => setView("week")}
          >
            Week
          </Button>
          <Button
            variant={view === "day" ? "default" : "outline"}
            size="sm"
            onClick={() => setView("day")}
          >
            Day
          </Button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4">
        {view === "week" ? (
          <div className="grid min-h-[28rem] grid-cols-1 gap-2 sm:grid-cols-7">
            {days.map((day) => {
              const items = occurrencesByDay.get(startOfDay(day).toISOString()) ?? [];
              return (
                <button
                  key={day.toISOString()}
                  type="button"
                  onClick={() => onCreate(day)}
                  className="flex min-h-48 cursor-pointer flex-col rounded-md border border-border bg-card p-3 text-left transition-colors hover:border-primary/50 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span className="text-sm font-medium">{dayFormatter.format(day)}</span>
                  <span className="mt-1 text-xs text-muted-foreground">
                    {items.length} scheduled
                  </span>
                  <span className="mt-3 flex flex-col gap-1">
                    {items.slice(0, 4).map((item) => (
                      <span
                        key={item.id}
                        className="rounded border border-border bg-background px-2 py-1 text-xs text-foreground"
                      >
                        {timeFormatter.format(new Date(item.fire_at * 1000))} {item.title}
                      </span>
                    ))}
                    {items.length > 4 && (
                      <span className="text-xs text-muted-foreground">
                        +{items.length - 4} more
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>
        ) : (
          <DayView day={calendarDate} occurrences={occurrences} onCreate={onCreate} />
        )}
        {loading && <p className="mt-3 text-xs text-muted-foreground">Loading schedules…</p>}
      </div>
    </div>
  );
}