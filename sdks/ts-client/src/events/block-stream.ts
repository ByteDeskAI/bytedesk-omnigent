// Folds the raw ServerStreamEvent stream into semantic OmnigentBlock values — the
// TS port of the Python client's BlockStream (`_stream.py`) and the C# SDK's
// OmnigentBlockStream.CollateAsync. The state machine mirrors them exactly so all
// three SDKs render identical conversation fidelity: text deltas collate into
// TextChunk then TextDone; reasoning deltas into reasoning blocks; function_call /
// function_call_output output items into ToolGroup / ToolResult; and
// session.child_session.updated into Delegation (the spawn tree is never dropped).

import type {
  ServerStreamEvent,
  UnknownEvent,
  ResponseObject,
} from "../generated/server-stream-events.js";
import type {
  BlockContext,
  OmnigentBlock,
  ToolExecution,
  ToolResultBlock,
  DelegationBlock,
} from "./blocks.js";

const TEXT_FLUSH_THRESHOLD = 30;

// Native (provider) tool item types — mirrors _events.NATIVE_TOOL_TYPES.
const NATIVE_TOOL_TYPES: ReadonlySet<string> = new Set([
  "web_search_call",
  "file_search_call",
  "code_interpreter_call",
  "computer_call",
  "image_generation_call",
  "mcp_call",
  "mcp_list_tools",
]);

// Per-tool argument key for the one-line summary (mirrors format_tool_args_brief).
const TOOL_ARG_KEYS: Readonly<Record<string, string>> = {
  Read: "file_path",
  Write: "file_path",
  Edit: "file_path",
  Bash: "command",
  Glob: "pattern",
  Grep: "pattern",
  web_search: "query",
};

/**
 * Collate a raw event stream into semantic blocks. The order of blocks mirrors
 * the Python + C# SDKs so all clients render identical conversation fidelity.
 * {@link UnknownEvent}s flow through untouched (they carry no semantics) so an
 * unknown frame never aborts collation.
 */
export async function* collateBlocks(
  events: AsyncIterable<ServerStreamEvent | UnknownEvent>,
): AsyncGenerator<OmnigentBlock> {
  let inReasoning = false;
  let reasoningText = "";
  let summaryText = "";
  let reasoningAccumulated = "";
  let reasoningChunksEmitted = false;
  let inText = false;
  let accumulated = "";
  let fullText = "";

  const pendingTools = new Map<string, ToolExecution>();
  const toolExecutionsByCallId = new Map<string, ToolExecution>();
  const seenCallIds = new Set<string>();
  const seenResultCallIds = new Set<string>();

  let agent: string | null = null;
  let turn = 0;
  let started = false;

  const ctx = (): BlockContext => ({
    agent,
    depth: agent ? countDots(agent) : 0,
    turn,
  });

  function* closeReasoning(): Generator<OmnigentBlock> {
    if (!inReasoning) return;
    inReasoning = false;
    if (reasoningAccumulated.length > 0) {
      yield { kind: "reasoning_chunk", ctx: ctx(), text: reasoningAccumulated };
      reasoningChunksEmitted = true;
      reasoningAccumulated = "";
    }
    if (!reasoningChunksEmitted) {
      yield { kind: "reasoning", ctx: ctx(), reasoningText, summaryText };
    }
  }

  function* closeText(): Generator<OmnigentBlock> {
    if (!inText) return;
    if (accumulated.length > 0) {
      yield { kind: "text_chunk", ctx: ctx(), text: accumulated };
      accumulated = "";
    }
    yield { kind: "text_done", ctx: ctx(), fullText, hasCodeBlocks: fullText.includes("```") };
    inText = false;
    fullText = "";
  }

  for await (const ev of events) {
    if ("kind" in ev && ev.kind === "unknown") {
      // UnknownEvent carries no block semantics — skip it (the raw reader already
      // surfaced it for callers that want it).
      continue;
    }
    const e = ev as ServerStreamEvent;

    switch (e.type) {
      // ── Response lifecycle ───────────────────────────────
      case "response.created": {
        for (const ex of [...pendingTools.values()]) {
          if (ex.output !== null) yield toolResult(ex, ctx());
        }
        pendingTools.clear();
        toolExecutionsByCallId.clear();
        agent = e.response.model;
        if (!started) {
          started = true;
          yield { kind: "response_start", ctx: ctx(), model: agent ?? "", responseId: e.response.id };
        } else {
          turn++;
        }
        break;
      }

      case "response.queued":
      case "response.in_progress":
        break;

      // ── Reasoning start ──────────────────────────────────
      case "response.reasoning.started": {
        if (inReasoning) {
          yield { kind: "reasoning_chunk", ctx: ctx(), text: reasoningAccumulated + "\n\n" };
          reasoningAccumulated = "";
          reasoningChunksEmitted = true;
          break;
        }
        yield* closeText();
        inReasoning = true;
        reasoningText = "";
        summaryText = "";
        reasoningAccumulated = "";
        reasoningChunksEmitted = false;
        yield { kind: "reasoning_start", ctx: ctx() };
        break;
      }

      // ── Reasoning deltas ─────────────────────────────────
      case "response.reasoning_text.delta":
      case "response.reasoning_summary_text.delta": {
        const delta = e.delta;
        const isText = e.type === "response.reasoning_text.delta";
        if (!inReasoning) {
          yield* closeText();
          inReasoning = true;
          reasoningText = "";
          summaryText = "";
          reasoningAccumulated = "";
          reasoningChunksEmitted = false;
          yield { kind: "reasoning_start", ctx: ctx() };
        }
        if (isText) reasoningText += delta;
        else summaryText += delta;
        reasoningAccumulated += delta;
        while (reasoningAccumulated.includes("\n")) {
          const nl = reasoningAccumulated.indexOf("\n");
          const line = reasoningAccumulated.slice(0, nl);
          reasoningAccumulated = reasoningAccumulated.slice(nl + 1);
          yield { kind: "reasoning_chunk", ctx: ctx(), text: line + "\n" };
          reasoningChunksEmitted = true;
        }
        if (reasoningAccumulated.length >= TEXT_FLUSH_THRESHOLD) {
          const lastSpace = reasoningAccumulated.lastIndexOf(" ");
          if (lastSpace > 0) {
            yield { kind: "reasoning_chunk", ctx: ctx(), text: reasoningAccumulated.slice(0, lastSpace + 1) };
            reasoningAccumulated = reasoningAccumulated.slice(lastSpace + 1);
            reasoningChunksEmitted = true;
          }
        }
        break;
      }

      // ── Text deltas ──────────────────────────────────────
      case "response.output_text.delta": {
        yield* closeReasoning();
        for (const ex of [...pendingTools.values()]) {
          if (ex.output !== null) yield toolResult(ex, ctx());
        }
        pendingTools.clear();

        inText = true;
        accumulated += e.delta;
        fullText += e.delta;
        while (accumulated.includes("\n")) {
          const nl = accumulated.indexOf("\n");
          const line = accumulated.slice(0, nl);
          accumulated = accumulated.slice(nl + 1);
          yield { kind: "text_chunk", ctx: ctx(), text: line + "\n" };
        }
        if (accumulated.length >= TEXT_FLUSH_THRESHOLD) {
          const lastSpace = accumulated.lastIndexOf(" ");
          if (lastSpace > 0) {
            yield { kind: "text_chunk", ctx: ctx(), text: accumulated.slice(0, lastSpace + 1) };
            accumulated = accumulated.slice(lastSpace + 1);
          }
        }
        break;
      }

      // ── Output items (function_call / function_call_output / message / native) ──
      case "response.output_item.done": {
        yield* handleOutputItem(
          e.item,
          ctx,
          closeReasoning,
          closeText,
          pendingTools,
          toolExecutionsByCallId,
          seenCallIds,
          seenResultCallIds,
        );
        break;
      }

      // ── Delegation / spawn tree ──────────────────────────
      case "session.child_session.updated": {
        yield delegation(e.conversation_id, e.child_session_id, e.child, ctx());
        break;
      }

      // ── Status events ────────────────────────────────────
      case "response.compaction.in_progress":
        yield { kind: "compaction", ctx: ctx() };
        break;

      case "response.retry":
        yield {
          kind: "retry",
          ctx: ctx(),
          source: e.source,
          attempt: e.attempt,
          maxAttempts: e.max_attempts,
          delaySeconds: e.delay_seconds,
        };
        break;

      case "response.error":
        yield {
          kind: "error",
          ctx: ctx(),
          message: e.error.message ?? "",
          source: e.source,
          code: e.error.code ?? "",
        };
        break;

      case "response.output_file.done":
        yield {
          kind: "file",
          ctx: ctx(),
          fileId: e.file_id,
          filename: e.filename ?? null,
          contentType: e.content_type ?? null,
        };
        break;

      // ── Terminal events ──────────────────────────────────
      case "response.completed":
      case "response.failed":
      case "response.incomplete":
      case "response.cancelled": {
        yield* closeReasoning();
        yield* closeText();
        const response: ResponseObject = e.response;
        yield { kind: "response_end", ctx: ctx(), status: response.status, response };
        break;
      }

      // All other events carry no block semantics.
      default:
        break;
    }
  }
}

function* handleOutputItem(
  item: Record<string, unknown>,
  ctx: () => BlockContext,
  closeReasoning: () => Generator<OmnigentBlock>,
  closeText: () => Generator<OmnigentBlock>,
  pendingTools: Map<string, ToolExecution>,
  toolExecutionsByCallId: Map<string, ToolExecution>,
  seenCallIds: Set<string>,
  seenResultCallIds: Set<string>,
): Generator<OmnigentBlock> {
  if (typeof item !== "object" || item === null) return;
  const itemType = stringProp(item, "type");

  switch (itemType) {
    case "function_call": {
      const callId = stringProp(item, "call_id");
      const name = stringProp(item, "name");
      const agentName = stringProp(item, "model");
      const args = parseArguments(item);

      if (seenCallIds.has(callId)) {
        // MCP path re-arrival: re-register so a later result still pairs, but
        // don't re-render the call line.
        let execution = toolExecutionsByCallId.get(callId);
        if (!execution) {
          execution = {
            name,
            arguments: args,
            argsSummary: formatToolArgsBrief(name, args),
            callId,
            agentName,
            executedBy: "server",
            output: null,
          };
          toolExecutionsByCallId.set(callId, execution);
        }
        pendingTools.set(callId, execution);
        return;
      }
      seenCallIds.add(callId);

      yield* closeReasoning();
      yield* closeText();

      const newExecution: ToolExecution = {
        name,
        arguments: args,
        argsSummary: formatToolArgsBrief(name, args),
        callId,
        agentName,
        executedBy: "server",
        output: null,
      };
      pendingTools.set(callId, newExecution);
      toolExecutionsByCallId.set(callId, newExecution);
      yield { kind: "tool_group", ctx: ctx(), executions: [newExecution], iteration: 0 };
      break;
    }

    case "function_call_output": {
      const callId = stringProp(item, "call_id");
      if (seenResultCallIds.has(callId)) return;
      const ex = pendingTools.get(callId) ?? toolExecutionsByCallId.get(callId);
      if (!ex) return;
      ex.output = stringProp(item, "output");
      ex.executedBy = "client";
      if ("arguments" in item) {
        const parsed = parseArgumentsValue(item["arguments"]);
        if (parsed !== null) ex.arguments = parsed;
      }
      seenResultCallIds.add(callId);
      yield toolResult(ex, ctx());
      pendingTools.delete(callId);
      break;
    }

    case "message": {
      yield* closeReasoning();
      const closed = [...closeText()];
      if (closed.length > 0) {
        yield* closed;
        break;
      }
      const text = outputTextFromMessage(item);
      if (text.length > 0) {
        yield { kind: "text_chunk", ctx: ctx(), text };
        yield { kind: "text_done", ctx: ctx(), fullText: text, hasCodeBlocks: text.includes("```") };
      }
      break;
    }

    default:
      if (NATIVE_TOOL_TYPES.has(itemType)) {
        yield {
          kind: "native_tool",
          ctx: ctx(),
          toolType: itemType,
          label: formatNativeLabel(itemType, item),
          data: item,
        };
      }
      break;
  }
}

function toolResult(ex: ToolExecution, ctx: BlockContext): ToolResultBlock {
  return {
    kind: "tool_result",
    ctx,
    name: ex.name,
    callId: ex.callId,
    agentName: ex.agentName,
    output: ex.output ?? "",
    arguments: ex.arguments,
    argsSummary: ex.argsSummary,
  };
}

function delegation(
  conversationId: string,
  childSessionId: string,
  child: Record<string, unknown>,
  ctx: BlockContext,
): DelegationBlock {
  let agentName: string | null = null;
  let status: string | null = null;
  if (typeof child === "object" && child !== null) {
    const a = child["agent_name"];
    if (typeof a === "string") agentName = a;
    const s = child["status"];
    if (typeof s === "string") status = s;
  }
  return {
    kind: "delegation",
    ctx,
    parentSessionId: conversationId,
    childSessionId,
    childAgentName: agentName,
    status,
    child: typeof child === "object" && child !== null ? child : {},
  };
}

// ── Native label (mirrors _stream native label formatting) ──
function formatNativeLabel(toolType: string, data: Record<string, unknown>): string {
  if (toolType === "web_search_call") {
    const action = data["action"];
    if (typeof action === "object" && action !== null) {
      const a = action as Record<string, unknown>;
      const at = typeof a["type"] === "string" ? (a["type"] as string) : "";
      if (at === "search") {
        return "web search: " + truncate(typeof a["query"] === "string" ? (a["query"] as string) : "", 80);
      }
      if (at === "open_page") {
        return "web open: " + truncate(typeof a["url"] === "string" ? (a["url"] as string) : "", 80);
      }
      return "web search";
    }
  }
  if (toolType === "mcp_call") {
    const n = typeof data["name"] === "string" ? (data["name"] as string) : "";
    return n.length > 0 ? "mcp: " + n : "mcp call";
  }
  return toolType.replace(/_/g, " ");
}

// ── Arg summary (mirrors _stream.format_tool_args_brief) ──
export function formatToolArgsBrief(name: string, args: Record<string, unknown>): string {
  if (typeof args !== "object" || args === null) return "";
  const keys = Object.keys(args);
  if (keys.length === 0) return "";

  const key = TOOL_ARG_KEYS[name];
  if (key !== undefined && key in args) {
    const val = args[key];
    let s = typeof val === "string" ? val : JSON.stringify(val);
    if (key === "file_path" && s.includes("/")) {
      s = s.slice(s.lastIndexOf("/") + 1);
    }
    return truncate(s, 80);
  }
  return truncate(JSON.stringify(args), 80);
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + "…" : s;
}

// ── JSON helpers ─────────────────────────────────────────
function stringProp(obj: Record<string, unknown>, name: string): string {
  const v = obj[name];
  return typeof v === "string" ? v : "";
}

function parseArguments(item: Record<string, unknown>): Record<string, unknown> {
  if (!("arguments" in item)) return {};
  return parseArgumentsValue(item["arguments"]) ?? {};
}

function parseArgumentsValue(raw: unknown): Record<string, unknown> | null {
  if (typeof raw === "object" && raw !== null && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  if (typeof raw === "string") {
    if (raw.length === 0) return null;
    try {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return null;
    }
  }
  return null;
}

function outputTextFromMessage(item: Record<string, unknown>): string {
  const content = item["content"];
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
  for (const block of content) {
    if (typeof block !== "object" || block === null) continue;
    const b = block as Record<string, unknown>;
    if (b["type"] !== "output_text") continue;
    if (typeof b["text"] === "string") parts.push(b["text"] as string);
  }
  return parts.join("");
}

function countDots(s: string): number {
  let n = 0;
  for (const c of s) if (c === ".") n++;
  return n;
}
