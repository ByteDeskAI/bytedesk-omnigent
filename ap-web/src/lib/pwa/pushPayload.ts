export type PushAttentionKind = "turn_end" | "elicitation";

export interface PushNotificationData {
  sessionId: string;
  kind: PushAttentionKind;
  url: string;
}

export function buildNotificationClickUrl(sessionId: string): string {
  return `/c/${sessionId}`;
}

export function buildPushNotificationPayload(params: {
  sessionId: string;
  title: string;
  body: string;
  kind: PushAttentionKind;
}): PushNotificationData & { title: string; body: string } {
  return {
    sessionId: params.sessionId,
    kind: params.kind,
    title: params.title,
    body: params.body,
    url: buildNotificationClickUrl(params.sessionId),
  };
}

/** Parse push event JSON from the service worker message. */
export function parsePushEventData(raw: unknown): PushNotificationData | null {
  if (raw == null || typeof raw !== "object") return null;
  const data = raw as Record<string, unknown>;
  const sessionId = data.sessionId ?? data.session_id;
  const kind = data.kind;
  const url = data.url;
  if (typeof sessionId !== "string" || !sessionId) return null;
  if (kind !== "turn_end" && kind !== "elicitation") return null;
  const resolvedUrl = typeof url === "string" && url ? url : buildNotificationClickUrl(sessionId);
  return { sessionId, kind, url: resolvedUrl };
}

/**
 * Client-side dedupe: skip showing a push when a focused visible client is
 * already on the target session (mirrors useIdleNotifications suppression).
 */
export function shouldSuppressPushForFocusedClient(
  clients: Array<{ focused: boolean; visibilityState: DocumentVisibilityState; url: string }>,
  sessionId: string,
  origin: string,
): boolean {
  const targetPath = buildNotificationClickUrl(sessionId);
  return clients.some((client) => {
    if (!client.focused || client.visibilityState !== "visible") return false;
    try {
      const url = new URL(client.url);
      if (url.origin !== origin) return false;
      return url.pathname === targetPath || url.pathname.endsWith(targetPath);
    } catch {
      return false;
    }
  });
}