export function rebaseUrl(serverUrl: string): string {
  try {
    const parsed = new URL(serverUrl);
    return `${window.location.origin}${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return serverUrl;
  }
}

export function formatEpoch(epoch: number | null): string {
  if (epoch === null) return "Never";
  const d = new Date(epoch * 1000);
  return d.toLocaleString();
}

export function formatTtl(expiresAt: number | undefined): string {
  if (expiresAt === undefined) return "soon";
  const secs = Math.max(0, expiresAt - Math.floor(Date.now() / 1000));
  const hours = Math.round(secs / 3600);
  if (hours >= 1) return `${hours}h`;
  return `${Math.max(1, Math.round(secs / 60))}m`;
}