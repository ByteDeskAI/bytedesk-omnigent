// Shared test fixtures: a captured turn (SSE frames) and helpers to build a fake
// fetch that returns an event-stream body.

/** Build a ReadableStream<Uint8Array> from a raw SSE string, optionally chunked oddly. */
export function sseStream(raw: string, chunkSize = 1_000_000): ReadableStream<Uint8Array> {
  const bytes = new TextEncoder().encode(raw);
  let offset = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (offset >= bytes.length) {
        controller.close();
        return;
      }
      const end = Math.min(offset + chunkSize, bytes.length);
      controller.enqueue(bytes.slice(offset, end));
      offset = end;
    },
  });
}

/** A fake fetch that returns a 200 text/event-stream response with the given body. */
export function fakeStreamFetch(raw: string, chunkSize?: number): typeof fetch {
  return (async () =>
    new Response(sseStream(raw, chunkSize), {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    })) as unknown as typeof fetch;
}

/** A fake fetch that captures the request and returns a JSON body. */
export function fakeJsonFetch(
  body: unknown,
  capture?: (url: string, init: RequestInit) => void,
  status = 200,
): typeof fetch {
  return (async (url: string | URL, init: RequestInit = {}) => {
    capture?.(String(url), init);
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof fetch;
}

// A captured single turn, in wire order:
//   turn.started → response.created → reasoning.started → reasoning deltas →
//   output_text deltas → function_call → function_call_output →
//   session.child_session.updated → output_item.done (message) →
//   response.completed → turn.completed
//
// Each frame is `event: <type>\n` + `data: <json>\n\n` with a leading `id:` on
// some frames to exercise the Last-Event-ID cursor, plus a heartbeat and a
// comment line to exercise tolerance.
export const CAPTURED_TURN = [
  frame("turn.started", { type: "turn.started", session_id: "conv_1" }, "1"),
  ": this is an SSE comment line, ignored",
  frame("response.created", {
    type: "response.created",
    response: { id: "resp_1", object: "response", status: "in_progress", model: "maya", created_at: 1700000000 },
  }, "2"),
  frame("response.heartbeat", { type: "response.heartbeat", server_time: "2026-01-01T00:00:00Z" }),
  frame("response.reasoning.started", { type: "response.reasoning.started" }),
  frame("response.reasoning_text.delta", { type: "response.reasoning_text.delta", delta: "Let me think about this carefully now.\n" }),
  frame("response.output_text.delta", { type: "response.output_text.delta", delta: "Hello, " }),
  frame("response.output_text.delta", { type: "response.output_text.delta", delta: "world!\n" }),
  frame("response.output_item.done", {
    type: "response.output_item.done",
    item: {
      id: "fc_1",
      type: "function_call",
      status: "completed",
      name: "Read",
      model: "maya",
      call_id: "call_1",
      arguments: JSON.stringify({ file_path: "/repo/src/y.py" }),
    },
  }, "3"),
  frame("response.output_item.done", {
    type: "response.output_item.done",
    item: { id: "fco_1", type: "function_call_output", call_id: "call_1", output: "file contents here" },
  }, "4"),
  frame("session.child_session.updated", {
    type: "session.child_session.updated",
    conversation_id: "conv_1",
    child_session_id: "conv_child_1",
    child: { agent_name: "researcher", status: "running" },
  }, "5"),
  frame("response.output_item.done", {
    type: "response.output_item.done",
    item: {
      id: "msg_1",
      type: "message",
      role: "assistant",
      content: [{ type: "output_text", text: "Done." }],
    },
  }),
  frame("response.completed", {
    type: "response.completed",
    response: { id: "resp_1", object: "response", status: "completed", model: "maya", created_at: 1700000000 },
  }, "6"),
  frame("turn.completed", { type: "turn.completed", session_id: "conv_1" }, "7"),
  "data: [DONE]",
  "",
].join("\n");

function frame(eventType: string, data: unknown, id?: string): string {
  const lines: string[] = [];
  if (id !== undefined) lines.push(`id: ${id}`);
  lines.push(`event: ${eventType}`);
  lines.push(`data: ${JSON.stringify(data)}`);
  lines.push(""); // frame boundary
  return lines.join("\n");
}
