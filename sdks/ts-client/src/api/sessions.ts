// The sessions port — bind-or-resume, post a turn, read items / child sessions /
// snapshot, the runnable check. Thin typed wrappers over the Omnigent /v1/sessions
// route family. Owns idempotency-key + cursor mechanics only; never ByteDesk policy.
// TS port of the C# SDK's ISessions / OmnigentSessions.

import { HttpTransport, ensureOk } from "./http.js";

/**
 * Bind-or-resume request for `POST /v1/sessions`. Binds to an already-registered
 * agent by durable id. When {@link BindSessionRequest.externalKey} is set, a repeat
 * create with the same key returns the live session instead of a duplicate
 * (idempotency); the key is also sent as the `Idempotency-Key` header.
 */
export interface BindSessionRequest {
  /** Durable id of the agent to bind (e.g. `"ag_abc123"`). */
  readonly agentId: string;
  /** Stable correlation key for bind-or-resume idempotency. Omitted from the wire when absent. */
  readonly externalKey?: string;
  /** Optional human-readable session title. */
  readonly title?: string;
  /** Optional parent session id for a sub-agent spawn. */
  readonly parentSessionId?: string;
  /** Initial guardrails labels to set on the session. */
  readonly labels?: Readonly<Record<string, string>>;
}

/**
 * One input for `POST /v1/sessions/{id}/events`. Mirrors the server's
 * `SessionEventInput` (a `type` discriminator + a type-specific `data` payload).
 */
export interface SessionTurnInput {
  /** Event/input kind (e.g. `"message"`, `"function_call_output"`, `"interrupt"`). */
  readonly type: string;
  /** Type-specific payload (for `"message"`: `{role, content[]}`). */
  readonly data: unknown;
}

/**
 * Typed snapshot of a session — the subset of the server's `SessionResponse` the
 * SDK surfaces (identity, binding, status). Wire names are snake_case (pinned to
 * the omnigent schema).
 */
export interface OmnigentSessionSnapshot {
  readonly id: string;
  readonly agent_id?: string;
  readonly agent_name?: string;
  readonly status?: string;
  readonly created_at?: number;
}

// A session in a terminal/failed status cannot accept a new turn.
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(["failed", "cancelled", "canceled"]);

/** The sessions port (see module doc). */
export class Sessions {
  constructor(private readonly http: HttpTransport) {}

  /**
   * Bind to an existing agent (or resume the live session for a repeat
   * `externalKey`). `POST /v1/sessions`.
   */
  async bindOrResume(request: BindSessionRequest): Promise<OmnigentSessionSnapshot> {
    const body: Record<string, unknown> = { agent_id: request.agentId };
    if (request.externalKey !== undefined) body["external_key"] = request.externalKey;
    if (request.title !== undefined) body["title"] = request.title;
    if (request.parentSessionId !== undefined) body["parent_session_id"] = request.parentSessionId;
    if (request.labels !== undefined) body["labels"] = request.labels;

    const headers: Record<string, string> = {};
    // The external_key doubles as the Idempotency-Key header (server reads either).
    if (request.externalKey) headers["Idempotency-Key"] = request.externalKey;

    const res = await this.http.send("POST", "/v1/sessions", { operationId: "create_session" }, {
      body: JSON.stringify(body),
      headers,
    });
    await ensureOk(res, "bind-or-resume session");
    return (await res.json()) as OmnigentSessionSnapshot;
  }

  /**
   * Post a turn/input to a session. `POST /v1/sessions/{id}/events` (202). An
   * `idempotencyKey`, when supplied, is sent as the `Idempotency-Key` header so a
   * retried post is de-duplicated.
   */
  async postTurn(sessionId: string, turn: SessionTurnInput, idempotencyKey?: string): Promise<void> {
    requireId(sessionId, "sessionId");
    const headers: Record<string, string> = {};
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    const res = await this.http.send(
      "POST",
      `/v1/sessions/${encodeURIComponent(sessionId)}/events`,
      { operationId: "post_event" },
      { body: JSON.stringify({ type: turn.type, data: turn.data }), headers },
    );
    await ensureOk(res, "post session turn");
  }

  /** Read committed conversation items (chronological). `GET /v1/sessions/{id}/items`. */
  getItems(sessionId: string): Promise<ReadonlyArray<Record<string, unknown>>> {
    requireId(sessionId, "sessionId");
    return this.getDataArray(`/v1/sessions/${encodeURIComponent(sessionId)}/items`, "list_session_items");
  }

  /** Read the session's child (sub-agent) sessions. `GET /v1/sessions/{id}/child_sessions`. */
  getChildSessions(sessionId: string): Promise<ReadonlyArray<Record<string, unknown>>> {
    requireId(sessionId, "sessionId");
    return this.getDataArray(
      `/v1/sessions/${encodeURIComponent(sessionId)}/child_sessions`,
      "list_child_sessions",
    );
  }

  /** Fetch the current session snapshot. `GET /v1/sessions/{id}`. */
  async getSnapshot(sessionId: string): Promise<OmnigentSessionSnapshot> {
    requireId(sessionId, "sessionId");
    const res = await this.http.send(
      "GET",
      `/v1/sessions/${encodeURIComponent(sessionId)}`,
      { operationId: "get_session" },
    );
    await ensureOk(res, "get session snapshot");
    return (await res.json()) as OmnigentSessionSnapshot;
  }

  /**
   * Whether the session can accept a turn — `true` unless its status is a
   * terminal/failed state. A protocol-level readiness check (no ByteDesk policy).
   */
  async isRunnable(sessionId: string): Promise<boolean> {
    const snapshot = await this.getSnapshot(sessionId);
    return !TERMINAL_STATUSES.has((snapshot.status ?? "").toLowerCase());
  }

  private async getDataArray(
    path: string,
    operationId: string,
  ): Promise<ReadonlyArray<Record<string, unknown>>> {
    const res = await this.http.send("GET", path, { operationId });
    await ensureOk(res, "list session data");
    const doc: unknown = await res.json();
    if (typeof doc === "object" && doc !== null && Array.isArray((doc as Record<string, unknown>)["data"])) {
      return (doc as { data: Record<string, unknown>[] }).data;
    }
    return [];
  }
}

function requireId(value: string, name: string): void {
  if (!value || value.trim().length === 0) throw new Error(`${name} is required`);
}
