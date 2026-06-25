export interface ShareTargetParams {
  title?: string;
  text?: string;
  url?: string;
}

/** Parse GET /share?title=&text=&url= from the Web Share Target API. */
export function parseShareTargetSearch(search: string): ShareTargetParams {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const title = params.get("title") ?? undefined;
  const text = params.get("text") ?? undefined;
  const url = params.get("url") ?? undefined;
  return { title, text, url };
}

/** Build composer prefill text from inbound share params. */
export function buildComposerPrefillFromShare(params: ShareTargetParams): string {
  const parts: string[] = [];
  if (params.title?.trim()) parts.push(params.title.trim());
  if (params.text?.trim()) parts.push(params.text.trim());
  if (params.url?.trim()) parts.push(params.url.trim());
  return parts.join("\n\n");
}

export function hasShareTargetContent(params: ShareTargetParams): boolean {
  return Boolean(params.title?.trim() || params.text?.trim() || params.url?.trim());
}