import {
  buildNotificationClickUrl,
  parsePushEventData,
  shouldSuppressPushForFocusedClient,
} from "@/lib/pwa/pushPayload";

export interface SwClientLike {
  focused: boolean;
  visibilityState: DocumentVisibilityState;
  url: string;
  focus?: () => Promise<unknown>;
  navigate?: (url: string) => Promise<unknown>;
}

export interface SwPushDeps {
  origin: string;
  matchClients: () => Promise<SwClientLike[]>;
  showNotification: (title: string, options: NotificationOptions) => Promise<void>;
}

export interface SwNotificationClickDeps {
  origin: string;
  matchClients: () => Promise<SwClientLike[]>;
  openWindow: (url: string) => Promise<WindowClient | null>;
}

/** Core push handler logic used by src/sw.ts. */
export async function handlePushPayload(
  raw: unknown,
  deps: SwPushDeps,
): Promise<{ shown: boolean; title?: string; body?: string }> {
  const payload = parsePushEventData(raw);
  if (!payload) return { shown: false };

  const clients = await deps.matchClients();
  if (
    shouldSuppressPushForFocusedClient(
      clients.map((c) => ({
        focused: c.focused,
        visibilityState: c.visibilityState,
        url: c.url,
      })),
      payload.sessionId,
      deps.origin,
    )
  ) {
    return { shown: false };
  }

  const envelope = (typeof raw === "object" && raw !== null ? raw : {}) as {
    title?: string;
    body?: string;
  };
  const title = typeof envelope.title === "string" ? envelope.title : "Omnigent";
  const body =
    typeof envelope.body === "string"
      ? envelope.body
      : payload.kind === "elicitation"
        ? "Agent is asking for your input."
        : "Agent finished and is ready for your input.";

  await deps.showNotification(title, {
    body,
    tag: `omnigent:${payload.sessionId}:${payload.kind}`,
    data: payload,
  });
  return { shown: true, title, body };
}

/** Core notificationclick handler logic used by src/sw.ts. */
export async function handleNotificationClick(
  notificationData: unknown,
  deps: SwNotificationClickDeps,
): Promise<string> {
  const data = parsePushEventData(notificationData);
  const targetUrl = data?.url ?? (data ? buildNotificationClickUrl(data.sessionId) : "/");
  const absolute = new URL(targetUrl, deps.origin).href;

  const clients = await deps.matchClients();
  for (const client of clients) {
    if (client.url.startsWith(deps.origin)) {
      await client.focus?.();
      if (client.navigate) await client.navigate(absolute);
      return absolute;
    }
  }
  await deps.openWindow(absolute);
  return absolute;
}