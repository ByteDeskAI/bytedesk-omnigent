// Semantic block taxonomy — the TS port of the Python client's `_blocks.py` and
// the C# SDK's Events/OmnigentBlock.cs. The block stream folds the raw
// ServerStreamEvent union into these so a frontend renders identical conversation
// fidelity across all three SDKs.

import type { ResponseObject } from "../generated/server-stream-events.js";

/**
 * Metadata attached to every {@link OmnigentBlock}: which agent produced it, at
 * what spawn depth, and in which turn. The simple consumer ignores it; multi-agent
 * frontends route by {@link BlockContext.agent}.
 */
export interface BlockContext {
  /** Name of the agent that produced this block (e.g. `"coder.researcher"`); `null` for the root. */
  readonly agent: string | null;
  /** Nesting depth in the sub-agent tree; `0` for the root. */
  readonly depth: number;
  /** Turn number within the current response. */
  readonly turn: number;
}

/** An empty context (root agent, depth 0, turn 0). */
export const EMPTY_CONTEXT: BlockContext = { agent: null, depth: 0, turn: 0 };

/** A single tool call paired with its (eventual) result. The state machine pairs a call line with its result by {@link ToolExecution.callId}. */
export interface ToolExecution {
  /** Tool name (e.g. `"Read"`). */
  name: string;
  /** Parsed arguments object (decoded `function_call.arguments` JSON). */
  arguments: Record<string, unknown>;
  /** One-line summary of the arguments (e.g. `"y.py"`). */
  argsSummary: string;
  /** Server-assigned call id. */
  callId: string;
  /** Name of the agent that invoked the tool. */
  agentName: string;
  /** `"server"` or `"client"`. */
  executedBy: string;
  /** Tool output text, or `null` until available. */
  output: string | null;
}

/** Discriminated kinds of {@link OmnigentBlock}, keyed on `kind`. */
export type OmnigentBlockKind =
  | "response_start"
  | "response_end"
  | "tool_group"
  | "tool_result"
  | "native_tool"
  | "delegation"
  | "text_chunk"
  | "text_done"
  | "reasoning_start"
  | "reasoning_chunk"
  | "reasoning"
  | "error"
  | "retry"
  | "compaction"
  | "file";

interface BlockBase {
  readonly kind: OmnigentBlockKind;
  /** Producer context (agent, depth, turn). */
  readonly ctx: BlockContext;
}

// ── Response lifecycle ───────────────────────────────────

/** The response has started. Mirrors `ResponseStartBlock`. */
export interface ResponseStartBlock extends BlockBase {
  readonly kind: "response_start";
  /** Agent model name (e.g. `"coder"`). */
  readonly model: string;
  /** Server-assigned response id. */
  readonly responseId: string;
}

/** The response reached a terminal state. Mirrors `ResponseEndBlock`. */
export interface ResponseEndBlock extends BlockBase {
  readonly kind: "response_end";
  /** Terminal status (e.g. `"completed"`, `"failed"`). */
  readonly status: string;
  /** The final response object, or `null`. */
  readonly response: ResponseObject | null;
}

// ── Tool calls ───────────────────────────────────────────

/** A batch of tool calls from one iteration. Mirrors `ToolGroup`. */
export interface ToolGroupBlock extends BlockBase {
  readonly kind: "tool_group";
  /** The tool calls in this group. */
  readonly executions: ReadonlyArray<ToolExecution>;
  /** Iteration number within the response. */
  readonly iteration: number;
}

/** A tool result, emitted after the tool executes. Mirrors `ToolResultBlock`. */
export interface ToolResultBlock extends BlockBase {
  readonly kind: "tool_result";
  readonly name: string;
  readonly callId: string;
  readonly agentName: string;
  readonly output: string;
  /** Parsed arguments from the matching tool call (retained for result-only renderers). */
  readonly arguments: Record<string, unknown>;
  /** One-line summary of the matching tool call arguments. */
  readonly argsSummary: string;
}

/** A provider-native tool output (web_search, mcp, etc.). Mirrors `NativeToolBlock`. */
export interface NativeToolBlock extends BlockBase {
  readonly kind: "native_tool";
  /** Provider tool type (e.g. `"web_search_call"`). */
  readonly toolType: string;
  /** Human-readable label for display. */
  readonly label: string;
  /** Raw provider data. */
  readonly data: Record<string, unknown>;
}

// ── Delegation / spawn tree ──────────────────────────────

/**
 * A child (sub-agent) session update — the spawn tree, derived from
 * `session.child_session.updated`. Every child update flows through (not just the
 * first-seen one), carrying parent → child + the child's agent and status so a
 * frontend can render the live delegation tree.
 */
export interface DelegationBlock extends BlockBase {
  readonly kind: "delegation";
  /** The PARENT (carrier) session id. */
  readonly parentSessionId: string;
  /** The child session id. */
  readonly childSessionId: string;
  /** The child agent's display name, when the update carried it. */
  readonly childAgentName: string | null;
  /** The child session's status (e.g. `"running"`), when present. */
  readonly status: string | null;
  /** The full (partial) child summary the update carried, for richer rendering. */
  readonly child: Record<string, unknown>;
}

// ── Text ─────────────────────────────────────────────────

/** A flushed chunk of streamed text. Mirrors `TextChunk`. */
export interface TextChunkBlock extends BlockBase {
  readonly kind: "text_chunk";
  /** The text content of this chunk. */
  readonly text: string;
}

/** Complete text from a text-streaming section. Mirrors `TextDone`. */
export interface TextDoneBlock extends BlockBase {
  readonly kind: "text_done";
  /** The complete accumulated text. */
  readonly fullText: string;
  /** Whether the text contains fenced code blocks. */
  readonly hasCodeBlocks: boolean;
}

// ── Reasoning ────────────────────────────────────────────

/** Reasoning has started — show a thinking indicator. Mirrors `ReasoningStartBlock`. */
export interface ReasoningStartBlock extends BlockBase {
  readonly kind: "reasoning_start";
}

/** An incremental reasoning chunk. Mirrors `ReasoningChunk`. */
export interface ReasoningChunkBlock extends BlockBase {
  readonly kind: "reasoning_chunk";
  /** The incremental reasoning text. */
  readonly text: string;
}

/**
 * A completed reasoning block, emitted only when no {@link ReasoningChunkBlock}
 * streamed for this section (so renderers don't show the text twice). Mirrors
 * `ReasoningBlock`.
 */
export interface ReasoningBlock extends BlockBase {
  readonly kind: "reasoning";
  /** The raw reasoning text. */
  readonly reasoningText: string;
  /** A summary of the reasoning. */
  readonly summaryText: string;
}

// ── Status ───────────────────────────────────────────────

/** An error during the response. Mirrors `ErrorBlock`. */
export interface ErrorBlock extends BlockBase {
  readonly kind: "error";
  /** Free-form error message (may be empty — fall back to {@link ErrorBlock.code}). */
  readonly message: string;
  /** Where the error originated (e.g. `"llm"`). */
  readonly source: string;
  /** Machine-readable error code from the server's error payload. */
  readonly code: string;
}

/** The server is retrying. Mirrors `RetryBlock`. */
export interface RetryBlock extends BlockBase {
  readonly kind: "retry";
  /** What is being retried (e.g. `"tool"`). */
  readonly source: string;
  /** Current attempt number. */
  readonly attempt: number;
  /** Maximum retry attempts. */
  readonly maxAttempts: number;
  /** Delay before the next attempt. */
  readonly delaySeconds: number;
}

/** Conversation is being compacted. Mirrors `CompactionBlock`. */
export interface CompactionBlock extends BlockBase {
  readonly kind: "compaction";
}

/** A file artifact produced by the agent. Mirrors `FileBlock`. */
export interface FileBlock extends BlockBase {
  readonly kind: "file";
  /** Server-assigned file id. */
  readonly fileId: string;
  /** Original filename, or `null` if unknown. */
  readonly filename: string | null;
  /** MIME content type, or `null` if unknown. */
  readonly contentType: string | null;
}

/** Every semantic block emitted by {@link collateBlocks}, as a discriminated union keyed on `kind`. */
export type OmnigentBlock =
  | ResponseStartBlock
  | ResponseEndBlock
  | ToolGroupBlock
  | ToolResultBlock
  | NativeToolBlock
  | DelegationBlock
  | TextChunkBlock
  | TextDoneBlock
  | ReasoningStartBlock
  | ReasoningChunkBlock
  | ReasoningBlock
  | ErrorBlock
  | RetryBlock
  | CompactionBlock
  | FileBlock;
