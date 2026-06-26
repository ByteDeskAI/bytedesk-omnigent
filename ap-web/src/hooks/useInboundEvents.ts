import { useEffect, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";

export interface InboundEventDelta {
  type: string;
  idempotencyKey: string;
  source: string;
  eventType: string;
  status: string;
  occurredAt: number;
  receivedAt: number;
  duplicate: boolean;
}

interface RecentRow {
  idempotency_key: string;
  source: string;
  type: string;
  status: string;
  occurred_at: number;
  received_at: number;
}

const MAX_EVENTS = 200;

function toDelta(row: RecentRow): InboundEventDelta {
  return {
    type: "inbound.event",
    idempotencyKey: row.idempotency_key,
    source: row.source,
    eventType: row.type,
    status: row.status,
    occurredAt: row.occurred_at,
    receivedAt: row.received_at,
    duplicate: false,
  };
}

/**
 * The live inbound-event feed (ADR-0155): REST snapshot hydration + a streamed
 * tail of `inbound.event` deltas, deduped + capped. Mirrors `useGoalEvents`.
 */
export function useInboundEvents(enabled = true) {
  const [events, setEvents] = useState<InboundEventDelta[]>([]);
  const [feedEnabled, setFeedEnabled] = useState(true);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await authenticatedFetch("/v1/inbound/recent?limit=100");
        if (!res.ok) return;
        const data = (await res.json()) as { enabled: boolean; events: RecentRow[] };
        if (cancelled) return;
        setFeedEnabled(data.enabled);
        setEvents(data.events.map(toDelta));
      } catch {
        /* hydration is best-effort; the stream backfills */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();

    async function connect() {
      try {
        const res = await authenticatedFetch("/v1/inbound/events", { signal: controller.signal });
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
            const dataLine = chunk.split("\n").find((line) => line.startsWith("data:"));
            if (!dataLine) continue;
            const event = JSON.parse(dataLine.slice(5)) as InboundEventDelta;
            if (event.type === "inbound.event") {
              setEvents((prev) =>
                [event, ...prev.filter((e) => e.idempotencyKey !== event.idempotencyKey)].slice(
                  0,
                  MAX_EVENTS,
                ),
              );
            } else if (event.type === "inbound.disabled") {
              setFeedEnabled(false);
            }
          }
          await pump();
        }

        await pump();
      } catch (error) {
        if (!controller.signal.aborted) {
          console.warn("Inbound event stream disconnected", error);
        }
      }
    }

    void connect();
    return () => {
      controller.abort();
    };
  }, [enabled]);

  return { events, feedEnabled };
}
