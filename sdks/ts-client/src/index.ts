// @bytedesk/omnigent-sdk — public surface (ADR-0152).
//
// Independent, direct-to-omnigent TypeScript client. Zero runtime dependencies;
// generated from the same pinned omnigent schema as the C# and Python SDKs.

// ── Client facade + options ──────────────────────────────
export { OmnigentClient, createOmnigentClient } from "./client.js";
export type { OmnigentClientOptions } from "./client.js";

// ── Sessions port ────────────────────────────────────────
export { Sessions } from "./api/sessions.js";
export type {
  BindSessionRequest,
  SessionTurnInput,
  OmnigentSessionSnapshot,
} from "./api/sessions.js";

// ── Events port (SSE reader) ─────────────────────────────
export { Events, OmnigentSchemaMismatchError } from "./api/events.js";
export type { ReadStreamOptions } from "./api/events.js";

// ── Roster port ──────────────────────────────────────────
export { Roster } from "./api/roster.js";
export type {
  OmnigentAgent,
  OmnigentAgentManager,
  OmnigentAgentImage,
  OmnigentAgentImageUpdate,
  OmnigentAgentMutationResult,
} from "./api/roster.js";

// ── Generated ServerStreamEvent union ────────────────────
export {
  parseServerStreamEvent,
  SERVER_STREAM_EVENT_TYPES,
} from "./generated/server-stream-events.js";
export type {
  ServerStreamEvent,
  UnknownEvent,
} from "./generated/server-stream-events.js";

// ── Semantic block taxonomy + stream + transforms ────────
export { collateBlocks, formatToolArgsBrief } from "./events/block-stream.js";
export {
  pipe,
  skipBlocks,
  skipIntermediateEnds,
  mergeTextAcrossIterations,
  onlyAgent,
} from "./events/transforms.js";
export type { BlockTransform } from "./events/transforms.js";
export { EMPTY_CONTEXT } from "./events/blocks.js";
export type {
  OmnigentBlock,
  OmnigentBlockKind,
  BlockContext,
  ToolExecution,
  ResponseStartBlock,
  ResponseEndBlock,
  ToolGroupBlock,
  ToolResultBlock,
  NativeToolBlock,
  DelegationBlock,
  TextChunkBlock,
  TextDoneBlock,
  ReasoningStartBlock,
  ReasoningChunkBlock,
  ReasoningBlock,
  ErrorBlock,
  RetryBlock,
  CompactionBlock,
  FileBlock,
} from "./events/blocks.js";

// ── Auth: credential-provider seam + provider set ────────
export {
  NoneCredentialProvider,
  StaticHeaderCredentialProvider,
  BearerTokenCredentialProvider,
  DelegatingCredentialProvider,
  ChainCredentialProvider,
  EMPTY_REQUEST_CONTEXT,
} from "./auth/credentials.js";
export type {
  CredentialProvider,
  RequestContext,
  TokenFactory,
} from "./auth/credentials.js";
export {
  CachingTokenCredentialProvider,
  ClientCredentialsCredentialProvider,
  TokenExchangeCredentialProvider,
} from "./auth/oauth.js";
export type {
  OAuthTokenResponse,
  ClientCredentialsOptions,
  TokenExchangeOptions,
} from "./auth/oauth.js";

// ── Pinned contract identity (version-skew surface) ──────
export { CONTRACT_LOCK } from "./contract.js";
export type { ContractLock } from "./contract.js";

// ── Generated HTTP DTO types (the broader openapi surface) ──
export type { components, paths, operations } from "./generated/openapi-types.js";
