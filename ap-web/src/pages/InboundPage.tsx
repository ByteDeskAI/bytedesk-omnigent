import { InboxIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useInboundEvents, type InboundEventDelta } from "@/hooks/useInboundEvents";

// ADR-0155: the live "Inbound Events" feed — every external event (webhooks, email,
// signal deliveries) as it flows through the generic ingestion pipeline.

function statusVariant(status: string): "secondary" | "destructive" | "outline" {
  if (status === "fanned_out" || status === "received") return "secondary";
  if (status === "dead_lettered") return "destructive";
  return "outline";
}

function timeLabel(epoch: number): string {
  return new Date(epoch * 1000).toLocaleTimeString();
}

function InboundRow({ event }: { event: InboundEventDelta }) {
  return (
    <li className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <Badge variant="outline">{event.source}</Badge>
        <span className="truncate text-sm font-medium">{event.eventType}</span>
        {event.duplicate && <Badge variant="outline">duplicate</Badge>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Badge variant={statusVariant(event.status)}>{event.status}</Badge>
        <span className="text-xs text-muted-foreground">{timeLabel(event.receivedAt)}</span>
      </div>
    </li>
  );
}

export function InboundPage() {
  const { events, feedEnabled } = useInboundEvents();

  return (
    <div className="mx-auto flex h-full w-full max-w-3xl flex-col gap-3 p-4">
      <header className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-sm font-semibold">
          <InboxIcon className="size-4" /> Inbound Events
        </h1>
        <span className="text-xs text-muted-foreground">{events.length} recent</span>
      </header>

      {!feedEnabled && (
        <div className="rounded-md border border-dashed border-border px-3 py-3 text-sm text-muted-foreground">
          The inbound feed is disabled. Enable the <code>inbound.feed.enabled</code> flag to stream events.
        </div>
      )}

      {feedEnabled && events.length === 0 && (
        <div className="rounded-md border border-dashed border-border px-3 py-3 text-sm text-muted-foreground">
          No inbound events yet — webhooks and other external events will appear here as they arrive.
        </div>
      )}

      <ol className="space-y-1.5">
        {events.map((event) => (
          <InboundRow key={event.idempotencyKey} event={event} />
        ))}
      </ol>
    </div>
  );
}
