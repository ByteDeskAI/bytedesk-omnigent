// The roster/org port — read the full agent roster (cursor-paginated) and the
// per-agent editable image over /v1/agents. Owns protocol mechanics (the
// has_more/last_id cursor walk, 404-as-null on image read) — never ByteDesk org
// policy. TS port of the C# SDK's IRoster / OmnigentRosterPort.

import { HttpTransport, ensureOk } from "./http.js";

/**
 * A manager reference on an omnigent agent. The manager refs are the raw bundle
 * params dict passed through by omnigent, so they are camelCase (`displayName`) —
 * unlike the snake_case agent-level fields.
 */
export interface OmnigentAgentManager {
  readonly id?: string;
  readonly displayName?: string;
  readonly title?: string;
}

/**
 * One omnigent agent from `GET /v1/agents`. Wire names pinned to openapi.json
 * (snake_case for the agent-level fields).
 *
 * `workflow` is deliberately `boolean | null`: omnigent emits `workflow: null` for
 * persona agents (only `true` for workflow agents), so consumers must tolerate
 * null — the gap that emptied the org chart in C# (BDP-2301).
 */
export interface OmnigentAgent {
  readonly id?: string;
  readonly name: string;
  readonly display_name?: string;
  readonly department?: string;
  readonly title?: string;
  readonly managers?: ReadonlyArray<OmnigentAgentManager>;
  readonly workflow?: boolean | null;
}

/**
 * A template agent's full editable image — `GET /v1/agents/{id}/image`. `config`
 * is the entire opaque AgentSpec surface, carried raw so the SDK never reprojects it.
 */
export interface OmnigentAgentImage {
  readonly id: string;
  readonly name: string;
  readonly version: number;
  readonly config: unknown;
  readonly instructions?: string;
  readonly skills?: ReadonlyArray<string>;
  readonly mcp_servers?: ReadonlyArray<string>;
  readonly python_tools?: ReadonlyArray<string>;
  readonly typescript_tools?: ReadonlyArray<string>;
  readonly sub_agents?: ReadonlyArray<string>;
  readonly sot_tier?: string;
}

/** Body for `PUT /v1/agents/{id}/image` — a partial edit; only supplied parts are overwritten. */
export interface OmnigentAgentImageUpdate {
  readonly config?: unknown;
  readonly instructions?: string;
  readonly files?: Readonly<Record<string, string>>;
  readonly remove?: ReadonlyArray<string>;
}

/** Subset of omnigent's `AgentObject` returned by `PUT …/image` — updated identity + bumped version. */
export interface OmnigentAgentMutationResult {
  readonly id: string;
  readonly name: string;
  readonly version: number;
}

// Page cap. The internal roster is bounded (~21 agents); 100 collapses it to a
// single page in practice while the cursor walk still handles any overflow.
const PAGE_SIZE = 100;

interface AgentPageWire {
  readonly data?: OmnigentAgent[];
  readonly has_more?: boolean;
  readonly last_id?: string;
}

/** The roster/org port (see module doc). */
export class Roster {
  constructor(private readonly http: HttpTransport) {}

  /**
   * Reads every built-in agent, walking the `has_more`/`last_id` cursor until the
   * list is exhausted. `GET /v1/agents`.
   */
  async getRoster(): Promise<ReadonlyArray<OmnigentAgent>> {
    const agents: OmnigentAgent[] = [];
    let after: string | null = null;

    do {
      const path =
        after === null
          ? `/v1/agents?limit=${PAGE_SIZE}`
          : `/v1/agents?limit=${PAGE_SIZE}&after=${encodeURIComponent(after)}`;

      const res = await this.http.send("GET", path, { operationId: "list_builtin_agents" });
      await ensureOk(res, "list agents");
      const page = (await res.json()) as AgentPageWire;
      if (page.data && page.data.length > 0) agents.push(...page.data);

      // Continue only when the server says there is more AND it handed back a
      // cursor — a missing/empty last_id ends the walk so we never loop.
      after = page.has_more && page.last_id ? page.last_id : null;
    } while (after !== null);

    return agents;
  }

  /**
   * Reads a template agent's full editable image by durable id, or `null` when
   * omnigent has no image for it (404). `GET /v1/agents/{id}/image`.
   */
  async getAgentImage(agentId: string): Promise<OmnigentAgentImage | null> {
    requireId(agentId);
    const res = await this.http.send(
      "GET",
      `/v1/agents/${encodeURIComponent(agentId)}/image`,
      { operationId: "get_agent_image" },
    );
    if (res.status === 404) return null;
    await ensureOk(res, "get agent image");
    return (await res.json()) as OmnigentAgentImage;
  }

  /** Rewrites a template agent's image (live, no restart). `PUT /v1/agents/{id}/image`. */
  async updateAgentImage(
    agentId: string,
    update: OmnigentAgentImageUpdate,
  ): Promise<OmnigentAgentMutationResult> {
    requireId(agentId);
    // Omit absent members from the wire so a partial PUT carries only supplied parts.
    const body: Record<string, unknown> = {};
    if (update.config !== undefined) body["config"] = update.config;
    if (update.instructions !== undefined) body["instructions"] = update.instructions;
    if (update.files !== undefined) body["files"] = update.files;
    if (update.remove !== undefined) body["remove"] = update.remove;

    const res = await this.http.send(
      "PUT",
      `/v1/agents/${encodeURIComponent(agentId)}/image`,
      { operationId: "put_agent_image" },
      { body: JSON.stringify(body) },
    );
    await ensureOk(res, "update agent image");
    return (await res.json()) as OmnigentAgentMutationResult;
  }
}

function requireId(agentId: string): void {
  if (!agentId || agentId.trim().length === 0) throw new Error("agentId is required");
}
