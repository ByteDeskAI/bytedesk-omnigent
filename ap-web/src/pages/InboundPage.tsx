import { InboxIcon } from "lucide-react";
import { InboundRow } from "@/components/inbound";
import { useInboundEvents } from "@/hooks/useInboundEvents";

// ADR-0155: the live "Inbound Events" feed — every external event (webhooks, email,
// signal deliveries) as it flows through the generic ingestion pipeline.

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

      <ol className="mc-stagger-children space-y-1.5">
        {events.map((event) => (
          <InboundRow key={event.idempotencyKey} event={event} />
        ))}
      </ol>
    </div>
  );
}