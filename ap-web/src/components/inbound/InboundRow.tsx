import { Badge } from "@/components/ui/badge";
import type { InboundEventDelta } from "@/hooks/useInboundEvents";
import { inboundStatusVariant, inboundTimeLabel } from "./inboundUtils";

export function InboundRow({ event }: { event: InboundEventDelta }) {
  return (
    <li className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <Badge variant="outline">{event.source}</Badge>
        <span className="truncate text-sm font-medium">{event.eventType}</span>
        {event.duplicate && <Badge variant="outline">duplicate</Badge>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Badge variant={inboundStatusVariant(event.status)}>{event.status}</Badge>
        <span className="text-xs text-muted-foreground">{inboundTimeLabel(event.receivedAt)}</span>
      </div>
    </li>
  );
}