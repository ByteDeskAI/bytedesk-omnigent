import { describe, it, expect } from "vitest";
import { createOmnigentClient } from "../src/index.js";
import type {
  OmnigentBlock,
  ToolGroupBlock,
  ToolResultBlock,
  DelegationBlock,
  TextDoneBlock,
} from "../src/index.js";
import { CAPTURED_TURN, fakeStreamFetch } from "./fixtures.js";

async function collectBlocks(it: AsyncIterable<OmnigentBlock>): Promise<OmnigentBlock[]> {
  const out: OmnigentBlock[] = [];
  for await (const b of it) out.push(b);
  return out;
}

describe("collateBlocks over a captured turn", () => {
  it("folds the raw stream into the expected semantic block sequence", async () => {
    const client = createOmnigentClient({ baseUrl: "https://omni.test", fetch: fakeStreamFetch(CAPTURED_TURN) });
    const blocks = await collectBlocks(client.events.readBlocks("conv_1"));
    const kinds = blocks.map((b) => b.kind);

    expect(kinds).toEqual([
      "response_start", // from response.created (model "maya")
      "reasoning_start",
      "reasoning_chunk", // the reasoning line flushed on newline
      "text_chunk", // "Hello, world!\n" flushed on the newline
      "text_done", // closeText() fired at the function_call (prior streamed text closed)
      "tool_group", // function_call
      "tool_result", // function_call_output
      "delegation", // session.child_session.updated
      "text_chunk", // message item text "Done."
      "text_done", // message item text done "Done."
      "response_end", // response.completed
    ]);
  });

  it("response_start carries the agent model", async () => {
    const client = createOmnigentClient({ baseUrl: "https://omni.test", fetch: fakeStreamFetch(CAPTURED_TURN) });
    const blocks = await collectBlocks(client.events.readBlocks("conv_1"));
    const start = blocks[0]!;
    expect(start.kind).toBe("response_start");
    if (start.kind === "response_start") {
      expect(start.model).toBe("maya");
      expect(start.responseId).toBe("resp_1");
    }
  });

  it("tool block carries structured (parsed) args + a brief summary", async () => {
    const client = createOmnigentClient({ baseUrl: "https://omni.test", fetch: fakeStreamFetch(CAPTURED_TURN) });
    const blocks = await collectBlocks(client.events.readBlocks("conv_1"));
    const group = blocks.find((b) => b.kind === "tool_group") as ToolGroupBlock;
    expect(group.executions).toHaveLength(1);
    const ex = group.executions[0]!;
    expect(ex.name).toBe("Read");
    // The JSON-string arguments were parsed into a structured object.
    expect(ex.arguments).toEqual({ file_path: "/repo/src/y.py" });
    // Read summary is the basename of file_path.
    expect(ex.argsSummary).toBe("y.py");

    const result = blocks.find((b) => b.kind === "tool_result") as ToolResultBlock;
    expect(result.callId).toBe("call_1");
    expect(result.output).toBe("file contents here");
    expect(result.arguments).toEqual({ file_path: "/repo/src/y.py" });
  });

  it("delegation block carries parent→child, child agent, status, and spawn depth", async () => {
    const client = createOmnigentClient({ baseUrl: "https://omni.test", fetch: fakeStreamFetch(CAPTURED_TURN) });
    const blocks = await collectBlocks(client.events.readBlocks("conv_1"));
    const deleg = blocks.find((b) => b.kind === "delegation") as DelegationBlock;
    expect(deleg.parentSessionId).toBe("conv_1");
    expect(deleg.childSessionId).toBe("conv_child_1");
    expect(deleg.childAgentName).toBe("researcher");
    expect(deleg.status).toBe("running");
    // The producing agent is "maya" (no dots) → depth 0.
    expect(deleg.ctx.agent).toBe("maya");
    expect(deleg.ctx.depth).toBe(0);
  });

  it("spawn depth = number of dots in the agent name", async () => {
    // A response.created with a dotted model name sets depth from the dot count.
    const raw =
      "event: response.created\ndata: " +
      JSON.stringify({
        type: "response.created",
        response: { id: "r", object: "response", status: "in_progress", model: "coder.researcher", created_at: 1 },
      }) +
      "\n\n" +
      "event: response.output_text.delta\ndata: " +
      JSON.stringify({ type: "response.output_text.delta", delta: "hi\n" }) +
      "\n\n" +
      "event: response.completed\ndata: " +
      JSON.stringify({
        type: "response.completed",
        response: { id: "r", object: "response", status: "completed", model: "coder.researcher", created_at: 1 },
      }) +
      "\n\n";
    const client = createOmnigentClient({ baseUrl: "https://omni.test", fetch: fakeStreamFetch(raw) });
    const blocks = await collectBlocks(client.events.readBlocks("conv_1"));
    const textChunk = blocks.find((b) => b.kind === "text_chunk")!;
    expect(textChunk.ctx.agent).toBe("coder.researcher");
    expect(textChunk.ctx.depth).toBe(1);
  });

  it("text_done blocks carry their full accumulated text", async () => {
    const client = createOmnigentClient({ baseUrl: "https://omni.test", fetch: fakeStreamFetch(CAPTURED_TURN) });
    const blocks = await collectBlocks(client.events.readBlocks("conv_1"));
    const dones = blocks.filter((b): b is TextDoneBlock => b.kind === "text_done");
    // Two text sections: the streamed deltas, then the trailing message item.
    expect(dones.map((d) => d.fullText)).toEqual(["Hello, world!\n", "Done."]);
    expect(dones.every((d) => d.hasCodeBlocks === false)).toBe(true);
  });
});
