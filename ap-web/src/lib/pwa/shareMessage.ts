export interface ShareMessageInput {
  title: string;
  text: string;
  url: string;
}

export async function shareMessageContent(input: ShareMessageInput): Promise<"shared" | "copied"> {
  if (typeof navigator.share === "function") {
    try {
      await navigator.share(input);
      return "shared";
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        return "copied";
      }
    }
  }
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(input.url);
    return "copied";
  }
  return "copied";
}