// The live event-stream port over `GET /v1/sessions/{id}/stream` (text/event-stream).
// Reads raw typed ServerStreamEvent frames, or folds them into semantic OmnigentBlocks.
// Owns SSE protocol mechanics — frame parsing, the `[DONE]` sentinel, heartbeat
// tolerance, the `Last-Event-ID` resume cursor — never ByteDesk policy. TS port of
// the C# SDK's IEvents / OmnigentEvents and the Python client's `_sse.py`.

import type { RequestContext } from "../auth/credentials.js";
import {
  parseServerStreamEvent,
  SERVER_STREAM_EVENT_TYPES,
  type ServerStreamEvent,
  type UnknownEvent,
} from "../generated/server-stream-events.js";
import { collateBlocks } from "../events/block-stream.js";
import type { OmnigentBlock } from "../events/blocks.js";
import { HttpTransport, ensureOk } from "./http.js";
import { readLines } from "./sse.js";

/** Thrown (when `throwOnUnknownEvent` is set) on the first frame whose wire `type` the pinned schema does not know. */
export class OmnigentSchemaMismatchError extends Error {
  readonly unknownType: string;
  constructor(unknownType: string) {
    super(
      `Omnigent emitted an event type the SDK's pinned schema does not know: '${unknownType}'. ` +
        `The server's event contract is newer than this SDK; regenerate the contract or set ` +
        `throwOnUnknownEvent=false to tolerate it.`,
    );
    this.name = "OmnigentSchemaMismatchError";
    this.unknownType = unknownType;
  }
}

export interface ReadStreamOptions {
  /** Credential scope hints applied per request. */
  readonly scope?: RequestContext;
  /** When resuming, the last seen `id:` cursor (sent as `Last-Event-ID`). */
  readonly lastEventId?: string;
  /** Cancellation. */
  readonly signal?: AbortSignal;
}

/** The events port (see module doc). */
export class Events {
  /** The most recent `id:` cursor observed on the stream, for resume. `null` until the first id arrives. */
  lastEventId: string | null = null;

  constructor(
    private readonly http: HttpTransport,
    private readonly throwOnUnknownEvent: boolean,
  ) {}

  /**
   * Open the session stream and yield each typed event in wire order. Skips the
   * typeless `data: [DONE]` sentinel (it terminates the stream), tolerates
   * heartbeats as ordinary events, tracks the `id:` cursor ({@link Events.lastEventId}),
   * and — per `throwOnUnknownEvent` — either surfaces an unknown frame as
   * {@link UnknownEvent} (default) or throws {@link OmnigentSchemaMismatchError}.
   */
  async *readRaw(
    sessionId: string,
    options: ReadStreamOptions = {},
  ): AsyncGenerator<ServerStreamEvent | UnknownEvent> {
    if (!sessionId || sessionId.trim().length === 0) throw new Error("sessionId is required");

    const headers: Record<string, string> = {};
    if (options.lastEventId) headers["Last-Event-ID"] = options.lastEventId;

    const res = await this.http.send(
      "GET",
      `/v1/sessions/${encodeURIComponent(sessionId)}/stream`,
      options.scope ?? {},
      { accept: "text/event-stream", headers, ...(options.signal ? { signal: options.signal } : {}) },
    );
    await ensureOk(res, "open session stream");
    if (!res.body) throw new Error("Omnigent session stream returned no body.");

    let currentEvent: string | null = null;
    let currentId: string | null = null;

    for await (const line of readLines(res.body)) {
      if (line.startsWith("event:")) {
        currentEvent = line.slice(6).replace(/^ /, "");
      } else if (line.startsWith("id:")) {
        currentId = line.slice(3).replace(/^ /, "");
      } else if (line.startsWith("data:")) {
        const data = line.slice(5).replace(/^ /, "");
        if (data === "[DONE]") return;

        // A data line is only a parseable event when an `event:` line preceded it
        // (mirrors _sse.py). A lone data line is ignored.
        if (currentEvent === null) continue;

        const parsed = this.deserialize(currentEvent, data);
        if (currentId !== null) this.lastEventId = currentId;
        currentEvent = null;
        if (parsed !== null) yield parsed;
      } else if (line.length === 0) {
        // Blank line = frame boundary. Reset per-frame event type.
        currentEvent = null;
      }
      // `:`-prefixed comment lines and unknown fields are ignored.
    }
  }

  /** Open the session stream and yield collated semantic blocks. */
  readBlocks(sessionId: string, options: ReadStreamOptions = {}): AsyncGenerator<OmnigentBlock> {
    return collateBlocks(this.readRaw(sessionId, options));
  }

  private deserialize(eventType: string, data: string): ServerStreamEvent | UnknownEvent | null {
    let json: unknown;
    try {
      json = JSON.parse(data);
    } catch {
      // A genuinely malformed frame is skipped (return null) rather than tearing
      // the stream down — matches the C# "known type, bad payload → skip" path.
      const wireType = eventType;
      if (SERVER_STREAM_EVENT_TYPES.has(wireType)) return null;
      if (this.throwOnUnknownEvent) throw new OmnigentSchemaMismatchError(wireType);
      return { kind: "unknown", type: wireType, raw: {} };
    }

    const result = parseServerStreamEvent(json);
    if ("kind" in result && result.kind === "unknown") {
      // Unknown discriminator → version skew. Prefer the parsed `type`, fall back
      // to the SSE `event:` field.
      const wireType = result.type || eventType;
      if (this.throwOnUnknownEvent) throw new OmnigentSchemaMismatchError(wireType);
      return { kind: "unknown", type: wireType, raw: result.raw };
    }
    return result;
  }
}
