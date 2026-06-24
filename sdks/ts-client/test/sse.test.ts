import { describe, it, expect } from "vitest";
import { createOmnigentClient } from "../src/index.js";
import type { ServerStreamEvent, UnknownEvent } from "../src/index.js";
import { CAPTURED_TURN, fakeStreamFetch, sseStream } from "./fixtures.js";
import { readLines } from "../src/api/sse.js";

async function collect(
  it: AsyncIterable<ServerStreamEvent | UnknownEvent>,
): Promise<(ServerStreamEvent | UnknownEvent)[]> {
  const out: (ServerStreamEvent | UnknownEvent)[] = [];
  for await (const ev of it) out.push(ev);
  return out;
}

describe("SSE line reader", () => {
  it("splits multi-line frames across odd chunk boundaries", async () => {
    const raw = "id: 1\nevent: a\ndata: x\n\nid: 2\nevent: b\ndata: y\n";
    // 1-byte chunks: forces the line buffer to reassemble across reads.
    const lines: string[] = [];
    for await (const l of readLines(sseStream(raw, 1))) lines.push(l);
    expect(lines).toEqual(["id: 1", "event: a", "data: x", "", "id: 2", "event: b", "data: y"]);
  });

  it("handles CRLF line endings", async () => {
    const raw = "event: a\r\ndata: x\r\n\r\n";
    const lines: string[] = [];
    for await (const l of readLines(sseStream(raw, 3))) lines.push(l);
    expect(lines).toEqual(["event: a", "data: x", ""]);
  });
});

describe("Events.readRaw over a captured turn", () => {
  it("parses the full typed event sequence, tolerates heartbeat + comment, skips [DONE]", async () => {
    const client = createOmnigentClient({
      baseUrl: "https://omni.test",
      fetch: fakeStreamFetch(CAPTURED_TURN),
    });
    const events = await collect(client.events.readRaw("conv_1"));
    const types = events.map((e) => ("kind" in e && e.kind === "unknown" ? `unknown:${e.type}` : (e as ServerStreamEvent).type));

    expect(types).toEqual([
      "turn.started",
      "response.created",
      "response.heartbeat", // heartbeat tolerated as an ordinary event
      "response.reasoning.started",
      "response.reasoning_text.delta",
      "response.output_text.delta",
      "response.output_text.delta",
      "response.output_item.done", // function_call
      "response.output_item.done", // function_call_output
      "session.child_session.updated",
      "response.output_item.done", // message
      "response.completed",
      "turn.completed",
    ]);
    // [DONE] terminated the stream — no further events, no UnknownEvent.
    expect(events.every((e) => !("kind" in e && e.kind === "unknown"))).toBe(true);
  });

  it("tracks the Last-Event-ID cursor from id: lines", async () => {
    const client = createOmnigentClient({
      baseUrl: "https://omni.test",
      fetch: fakeStreamFetch(CAPTURED_TURN),
    });
    await collect(client.events.readRaw("conv_1"));
    // Last frame carrying an id: was turn.completed → id 7.
    expect(client.events.lastEventId).toBe("7");
  });

  it("sends Last-Event-ID header when resuming", async () => {
    let sentHeader: string | null = null;
    const fetchSpy = (async (_url: string, init: RequestInit) => {
      const headers = new Headers(init.headers);
      sentHeader = headers.get("Last-Event-ID");
      return new Response(sseStream("data: [DONE]\n"), { status: 200 });
    }) as unknown as typeof fetch;
    const client = createOmnigentClient({ baseUrl: "https://omni.test", fetch: fetchSpy });
    for await (const _ of client.events.readRaw("conv_1", { lastEventId: "42" })) void _;
    expect(sentHeader).toBe("42");
  });

  it("surfaces an unknown event type as UnknownEvent (tolerant default)", async () => {
    const raw = "event: response.brand_new\ndata: {\"type\":\"response.brand_new\",\"x\":1}\n\ndata: [DONE]\n";
    const client = createOmnigentClient({ baseUrl: "https://omni.test", fetch: fakeStreamFetch(raw) });
    const events = await collect(client.events.readRaw("conv_1"));
    expect(events).toHaveLength(1);
    const ev = events[0]!;
    expect("kind" in ev && ev.kind === "unknown").toBe(true);
    const unknown = ev as UnknownEvent;
    expect(unknown.type).toBe("response.brand_new");
    expect(unknown.raw).toEqual({ type: "response.brand_new", x: 1 });
  });

  it("throws OmnigentSchemaMismatchError on unknown type in strict mode", async () => {
    const raw = "event: response.brand_new\ndata: {\"type\":\"response.brand_new\"}\n\n";
    const client = createOmnigentClient({
      baseUrl: "https://omni.test",
      fetch: fakeStreamFetch(raw),
      throwOnUnknownEvent: true,
    });
    await expect(collect(client.events.readRaw("conv_1"))).rejects.toThrow(/does not know/);
  });

  it("skips a malformed payload for a KNOWN type without tearing down", async () => {
    const raw =
      "event: response.output_text.delta\ndata: {not valid json\n\n" +
      "event: turn.completed\ndata: {\"type\":\"turn.completed\",\"session_id\":\"s\"}\n\n";
    const client = createOmnigentClient({ baseUrl: "https://omni.test", fetch: fakeStreamFetch(raw) });
    const events = await collect(client.events.readRaw("conv_1"));
    // The malformed known-type frame was skipped; the stream survived to the next.
    expect(events).toHaveLength(1);
    expect((events[0] as ServerStreamEvent).type).toBe("turn.completed");
  });
});
