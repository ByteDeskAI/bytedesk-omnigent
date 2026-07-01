import type { ScheduleOccurrence } from "@/lib/schedulesApi";
import { addHours, occurrenceHour, timeFormatter } from "./schedules-utils";

export function DayView({
  day,
  occurrences,
  onCreate,
}: {
  day: Date;
  occurrences: ScheduleOccurrence[];
  onCreate: (slot: Date) => void;
}) {
  return (
    <div className="grid gap-1">
      {Array.from({ length: 24 }, (_, hour) => {
        const slot = addHours(day, hour);
        const items = occurrences.filter((item) => occurrenceHour(item.fire_at) === hour);
        return (
          <button
            key={hour}
            type="button"
            onClick={() => onCreate(slot)}
            className="grid min-h-14 cursor-pointer grid-cols-[5rem_minmax(0,1fr)] items-stretch rounded-md border border-border bg-card text-left transition-colors hover:border-primary/50 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className="flex items-center border-r border-border px-3 text-xs text-muted-foreground">
              {timeFormatter.format(slot)}
            </span>
            <span className="flex min-w-0 flex-wrap items-center gap-1 px-3 py-2">
              {items.length === 0 ? (
                <span className="text-xs text-muted-foreground">Open</span>
              ) : (
                items.map((item) => (
                  <span
                    key={item.id}
                    className="rounded border border-border bg-background px-2 py-1 text-xs"
                  >
                    {item.title}
                  </span>
                ))
              )}
            </span>
          </button>
        );
      })}
    </div>
  );
}