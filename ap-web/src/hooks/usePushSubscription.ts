import { useEffect } from "react";
import { getNotificationPermission, requestNotificationPermission } from "@/lib/browserNotifications";
import { subscribeToPushNotifications } from "@/lib/pwa/pushSubscription";
import { shouldRegisterPwa } from "@/lib/pwa/runtime";

/**
 * After notification permission is granted, register the browser push
 * subscription with the Omnigent server (standalone PWA path only).
 */
export function usePushSubscription(): void {
  useEffect(() => {
    if (!shouldRegisterPwa()) return;
    if (!("serviceWorker" in navigator)) return;

    const sync = async () => {
      if (getNotificationPermission() !== "granted") return;
      await subscribeToPushNotifications();
    };

    void sync();

    const onGranted = () => {
      void sync();
    };
    window.addEventListener("pointerdown", onGranted, { once: true });
    window.addEventListener("keydown", onGranted, { once: true });
    return () => {
      window.removeEventListener("pointerdown", onGranted);
      window.removeEventListener("keydown", onGranted);
    };
  }, []);
}

export async function ensurePushAfterPermissionGrant(): Promise<void> {
  if (!shouldRegisterPwa()) return;
  const perm = await requestNotificationPermission();
  if (perm === "granted") await subscribeToPushNotifications();
}