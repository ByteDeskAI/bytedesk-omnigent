import { resolveWebSocketUrl } from "@/lib/host";

/**
 * Detect whether the current browser is running on macOS.
 */
export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const uaData = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData;
  const platform = uaData?.platform ?? navigator.platform ?? "";
  return /mac/i.test(platform);
}

/**
 * Build the persistent selection/copy hint shown under the terminal.
 */
export function selectionHintText(isMac: boolean): string {
  return isMac
    ? "Hold ⌥ and drag to select · ⌘C to copy"
    : "Hold Shift and drag to select · right-click to copy";
}

export function resumeErrorText(error: unknown): string {
  if (error instanceof Error && error.message) return `Couldn't resume session: ${error.message}`;
  return "Couldn't resume session.";
}

/**
 * Build the path + query for the resource-addressed attach endpoint.
 */
export function buildAttachPath(sessionId: string, terminalId: string, readOnly: boolean): string {
  const path =
    `/v1/sessions/${encodeURIComponent(sessionId)}` +
    `/resources/terminals/${encodeURIComponent(terminalId)}/attach`;
  const qs = readOnly ? "?read_only=true" : "";
  return `${path}${qs}`;
}

export function buildAttachUrl({
  sessionId,
  terminalId,
  readOnly,
  attachPath,
}: {
  sessionId?: string;
  terminalId?: string;
  readOnly: boolean;
  attachPath?: string;
}): string {
  if (attachPath) return resolveWebSocketUrl(attachPath);
  if (!sessionId || !terminalId) {
    throw new Error("buildAttachUrl requires sessionId + terminalId when attachPath is absent");
  }
  return resolveWebSocketUrl(buildAttachPath(sessionId, terminalId, readOnly));
}