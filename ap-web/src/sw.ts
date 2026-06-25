/// <reference lib="webworker" />
import { clientsClaim } from "workbox-core";
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";
import { registerRoute, NavigationRoute } from "workbox-routing";
import { NetworkOnly, StaleWhileRevalidate } from "workbox-strategies";
import {
  buildNotificationClickUrl,
  parsePushEventData,
  shouldSuppressPushForFocusedClient,
} from "./lib/pwa/pushPayload";

declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<{ url: string; revision: string | null }>;
};

precacheAndRoute(self.__WB_MANIFEST);
cleanupOutdatedCaches();
clientsClaim();

registerRoute(({ url }) => url.pathname === "/v1/info", new StaleWhileRevalidate({ cacheName: "omnigent-server-info" }));

registerRoute(
  ({ url }) =>
    url.pathname.startsWith("/v1/") ||
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/auth/"),
  new NetworkOnly(),
);

const navigationHandler = new NetworkOnly();
registerRoute(new NavigationRoute(navigationHandler));

self.addEventListener("push", (event: PushEvent) => {
  event.waitUntil(
    (async () => {
      let payload: ReturnType<typeof parsePushEventData> = null;
      try {
        const raw = event.data?.json();
        payload = parsePushEventData(raw);
      } catch {
        payload = null;
      }
      if (!payload) return;

      const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const origin = self.location.origin;
      if (
        shouldSuppressPushForFocusedClient(
          clients.map((c) => ({
            focused: c.focused,
            visibilityState: c.visibilityState,
            url: c.url,
          })),
          payload.sessionId,
          origin,
        )
      ) {
        return;
      }

      let envelope: { title?: string; body?: string } = {};
      try {
        envelope = (event.data?.json() ?? {}) as { title?: string; body?: string };
      } catch {
        envelope = {};
      }
      const title = typeof envelope.title === "string" ? envelope.title : "Omnigent";
      const body =
        typeof envelope.body === "string"
          ? envelope.body
          : payload.kind === "elicitation"
            ? "Agent is asking for your input."
            : "Agent finished and is ready for your input.";

      await self.registration.showNotification(title, {
        body,
        tag: `omnigent:${payload.sessionId}:${payload.kind}`,
        data: payload,
      });
    })(),
  );
});

self.addEventListener("notificationclick", (event: NotificationEvent) => {
  event.notification.close();
  const data = parsePushEventData(event.notification.data);
  const targetUrl = data?.url ?? (data ? buildNotificationClickUrl(data.sessionId) : "/");
  const absolute = new URL(targetUrl, self.location.origin).href;

  event.waitUntil(
    (async () => {
      const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of clients) {
        if (client.url.startsWith(self.location.origin)) {
          await client.focus();
          if ("navigate" in client && typeof client.navigate === "function") {
            await client.navigate(absolute);
          }
          return;
        }
      }
      await self.clients.openWindow(absolute);
    })(),
  );
});