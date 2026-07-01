import type { Comment } from "@/hooks/useComments";

/**
 * Classify comments into open/addressed and remap open draft comments to
 * their correct absolute offsets when the file content has changed.
 */
export function classifyAndRemapComments(
  comments: Comment[],
  fileContent: string,
): { open: Comment[]; addressed: Comment[] } {
  const open: Comment[] = [];
  const addressed: Comment[] = [];

  for (const c of comments) {
    if (c.status === "addressed") {
      addressed.push(c);
      continue;
    }
    if (!c.anchor_content) {
      open.push(c);
      continue;
    }
    if (!fileContent) {
      open.push(c);
      continue;
    }
    const SEARCH_WINDOW = 200;
    const windowStart = Math.max(0, c.start_index - SEARCH_WINDOW);
    const windowEnd = Math.min(
      fileContent.length,
      c.start_index + c.anchor_content.length + SEARCH_WINDOW,
    );
    const nearbyIdx = fileContent.indexOf(c.anchor_content, windowStart);
    const idx =
      nearbyIdx !== -1 && nearbyIdx <= windowEnd
        ? nearbyIdx
        : fileContent.indexOf(c.anchor_content);
    if (idx === -1) {
      open.push(c);
      continue;
    }
    if (idx !== c.start_index) {
      open.push({ ...c, start_index: idx, end_index: idx + c.anchor_content.length });
    } else {
      open.push(c);
    }
  }

  return { open, addressed };
}