import type { RenderItem } from "@/lib/renderItems";

export const STREAM_MARKDOWN_THROTTLE_MS = 100;
export const MAX_MARKDOWN_TEXT_LENGTH = 50_000;
export const MAX_UNBROKEN_TOKEN_LENGTH = 5_000;
export const MAX_PLAINTEXT_DISPLAY_LENGTH = 200_000;
export const STREAMING_TAIL = 3;

export function longestUnbrokenRun(text: string): number {
  let max = 0;
  let current = 0;
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    if (code === 32 || (code >= 9 && code <= 13)) {
      current = 0;
    } else {
      current += 1;
      if (current > max) max = current;
    }
  }
  return max;
}

export function isPathologicalText(text: string): boolean {
  return (
    text.length > MAX_MARKDOWN_TEXT_LENGTH || longestUnbrokenRun(text) > MAX_UNBROKEN_TOKEN_LENGTH
  );
}

export function partitionToolRun(
  run: RenderItem[],
  isStreamingRun: boolean,
): { grouped: RenderItem[]; standalone: RenderItem[] } {
  if (isStreamingRun) {
    const tailStart = Math.max(0, run.length - STREAMING_TAIL);
    return { grouped: run.slice(0, tailStart), standalone: run.slice(tailStart) };
  }
  return {
    grouped: run.filter((t) => !isInProgressTool(t)),
    standalone: run.filter(isInProgressTool),
  };
}

export function isToolItem(item: RenderItem): boolean {
  return item.kind === "tool" || item.kind === "native_tool";
}

export function findStreamingRunStart(items: RenderItem[]): number {
  if (items.length === 0) return -1;
  if (!isToolItem(items[items.length - 1]!)) return -1;
  let i = items.length - 1;
  while (i > 0 && isToolItem(items[i - 1]!)) i -= 1;
  return i;
}

export function isInProgressTool(item: RenderItem): boolean {
  return item.kind === "tool" && item.state === "input-available";
}

export function keyFor(item: RenderItem, index: number): string {
  if (item.itemId) return `${item.kind}:${item.itemId}`;
  if (item.kind === "tool") return `tool:${item.execution.callId}`;
  if (item.kind === "file") return `file:${item.fileId}`;
  if (item.kind === "elicitation") return `elicitation:${item.elicitationId}`;
  return `${item.kind}:${index}`;
}

export function isImageArtifact(item: Extract<RenderItem, { kind: "file" }>): boolean {
  if (item.contentType?.startsWith("image/")) return true;
  return /\.(png|jpe?g|webp|gif|svg)$/i.test(item.filename ?? "");
}