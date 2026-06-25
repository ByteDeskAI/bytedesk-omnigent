/// <reference lib="webworker" />
import { clientsClaim } from "workbox-core";
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";
import { registerRoute, NavigationRoute } from "workbox-routing";
import { NetworkOnly, StaleWhileRevalidate } from "workbox-strategies";
import { handleNotificationClick, handlePushPayload } from "./lib/pwa/swHandlers";

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
      let raw: unknown = null;
      try {
        raw = event.data?.json();
      } catch {
        raw = null;
      }
      await handlePushPayload(raw, {
        origin: self.location.origin,
        matchClients: async () => {
          const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
          return clients.map((c) => ({
            focused: c.focused,
            visibilityState: c.visibilityState,
            url: c.url,
          }));
        },
        showNotification: async (title, options) => {
          await self.registration.showNotification(title, options);
        },
      });
    })(),
  );
});

self.addEventListener("notificationclick", (event: NotificationEvent) => {
  event.notification.close();
  event.waitUntil(
    handleNotificationClick(event.notification.data, {
      origin: self.location.origin,
      matchClients: async () => {
        const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
        return clients.map((c) => ({
          focused: c.focused,
          visibilityState: c.visibilityState,
          url: c.url,
          focus: () => c.focus(),
          navigate:
            "navigate" in c && typeof c.navigate === "function"
              ? (url: string) => c.navigate!(url)
              : undefined,
        }));
      },
      openWindow: (url) => self.clients.openWindow(url),
    }),
  );
});