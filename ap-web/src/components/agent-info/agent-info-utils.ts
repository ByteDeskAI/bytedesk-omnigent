import type { ModelUsage } from "@/lib/types";
import { capitalizeAgentName } from "@/lib/agentLabels";
import { agentRootName } from "@/lib/forkHarness";
import { nativeCodingAgentForAgentName } from "@/lib/nativeCodingAgents";

/**
 * Display label for an agent name: the wrapper alias when mapped, else
 * the name capital-first (server agent names are lowercase slugs, e.g.
 * ``"polly"`` → ``"Polly"``). Keeps the chat surfaces consistent with
 * the new-chat picker's capitalization.
 */
export function agentDisplayLabel(name: string): string {
  const baseName = agentRootName(name);
  const nativeAgent = nativeCodingAgentForAgentName(baseName);
  if (nativeAgent?.key === "claude") return "Claude";
  return nativeAgent?.displayName ?? capitalizeAgentName(baseName);
}

/** Format cumulative session spend: `$x.xx`, or `<$0.01` for sub-cent. */
export function formatSessionCostUsd(costUsd: number): string {
  if (costUsd > 0 && costUsd < 0.01) {
    return "<$0.01";
  }
  return `$${costUsd.toFixed(2)}`;
}

/** Compact token-count formatter for the usage breakdown. */
export function formatTokenCount(tokens: number): string {
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(tokens);
}

export const MODEL_TOKEN_ROWS: ReadonlyArray<{ key: keyof ModelUsage; label: string }> = [
  { key: "inputTokens", label: "Input" },
  { key: "outputTokens", label: "Output" },
  { key: "cacheReadInputTokens", label: "Cache read" },
  { key: "cacheCreationInputTokens", label: "Cache write" },
  { key: "totalTokens", label: "Total" },
];