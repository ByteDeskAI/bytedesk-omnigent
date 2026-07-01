export function defaultForkTitle(sourceTitle: string | null | undefined): string {
  const trimmed = sourceTitle?.trim();
  return trimmed ? `Fork of ${trimmed}` : "";
}