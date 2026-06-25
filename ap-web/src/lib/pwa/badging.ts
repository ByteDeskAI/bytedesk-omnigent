import { isNativeShell, setBadgeCount as setElectronBadgeCount } from "@/lib/nativeBridge";

type NavigatorWithBadge = Navigator & {
  setAppBadge?: (count: number) => Promise<void>;
  clearAppBadge?: () => Promise<void>;
};

/**
 * Paint unread count on Electron dock, installed PWA (Badging API), or no-op.
 */
export async function setAppBadgeCount(count: number): Promise<void> {
  if (isNativeShell()) {
    await setElectronBadgeCount(count);
    return;
  }
  if (typeof navigator === "undefined") return;
  const nav = navigator as NavigatorWithBadge;
  try {
    if (count <= 0) {
      if (typeof nav.clearAppBadge === "function") await nav.clearAppBadge();
      return;
    }
    if (typeof nav.setAppBadge === "function") await nav.setAppBadge(count);
  } catch {
    // Badging API is optional; never break notifications.
  }
}