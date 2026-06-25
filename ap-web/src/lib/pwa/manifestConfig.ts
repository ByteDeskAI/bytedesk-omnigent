/** Manifest fields shared by vite-plugin-pwa and unit tests. */

export const PWA_THEME_COLOR = "#0a0a0a";
export const PWA_BACKGROUND_COLOR = "#0a0a0a";

export const manifestShortcuts = [
  {
    name: "New chat",
    short_name: "New",
    url: "/",
    icons: [{ src: "/pwa-192.png", sizes: "192x192", type: "image/png" }],
  },
  {
    name: "Inbox",
    short_name: "Inbox",
    url: "/inbox",
    icons: [{ src: "/pwa-192.png", sizes: "192x192", type: "image/png" }],
  },
  {
    name: "Approvals",
    short_name: "Approvals",
    url: "/inbox?filter=awaiting",
    icons: [{ src: "/pwa-192.png", sizes: "192x192", type: "image/png" }],
  },
] as const;

export const manifestScreenshots = [
  {
    src: "/screenshot-desktop.png",
    sizes: "1280x720",
    type: "image/png",
    form_factor: "wide" as const,
    label: "Omnigent desktop",
  },
  {
    src: "/screenshot-mobile.png",
    sizes: "390x844",
    type: "image/png",
    form_factor: "narrow" as const,
    label: "Omnigent mobile",
  },
] as const;

export const relatedApplications = [
  {
    platform: "webapp",
    url: "https://github.com/ByteDeskAI/bytedesk-omnigent/tree/develop/ap-web/electron",
    id: "omnigent-desktop",
  },
] as const;

export const shareTarget = {
  action: "/share",
  method: "GET" as const,
  params: {
    title: "title",
    text: "text",
    url: "url",
  },
};