// The SDK's pinned contract identity — the omnigent version + content hashes the
// generated code was produced against. TS port of the C# SDK's OmnigentContract
// (version-skew surface).
//
// `src/contract/contract.lock` is the canonical drift-gate artifact: it pins the
// sha256 of the two committed contract files and the omnigent git sha. The
// `contractDriftGate` test recomputes those hashes from the shipped contract files
// and asserts they equal both this constant AND contract.lock — so a contract
// refresh that forgets to re-lock fails the build. (The lock file itself isn't
// imported at runtime: it has no `.json` extension, so importing it would couple
// the SDK to a Node-only fs read; the constant + drift test keep them identical
// without that coupling, while staying browser/bundler portable.)

/** The pinned contract identity (sha256 of the openapi + event schema, + omnigent git sha). */
export interface ContractLock {
  /** sha256 over the committed `src/contract/openapi.json` bytes. */
  readonly openapi_sha256: string;
  /** sha256 over the committed `src/contract/server-stream-event.schema.json` bytes. */
  readonly events_sha256: string;
  /** Short omnigent git sha the snapshot was taken from. */
  readonly omnigent_version: string;
}

/** The SDK's pinned contract identity. Kept in lockstep with `src/contract/contract.lock` by the drift-gate test. */
export const CONTRACT_LOCK: ContractLock = {
  openapi_sha256: "fb73affe8073c8ff12103b4bcceadad385cbc55d8d63ca2247e6966507cd6612",
  events_sha256: "fa0233f67b304bfa2795cf33b8cab9dcb6c8baac25e45c8a8a16960f5cb92a4e",
  omnigent_version: "962a88fb",
};
