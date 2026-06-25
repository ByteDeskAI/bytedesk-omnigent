import { authenticatedFetch } from "@/lib/identity";
import { shouldRegisterPwa } from "@/lib/pwa/runtime";

export interface PushSubscriptionJson {
  endpoint: string;
  keys: { p256dh: string; auth: string };
}

function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
  return output;
}

export function subscriptionToJson(sub: PushSubscription): PushSubscriptionJson | null {
  const json = sub.toJSON();
  const endpoint = json.endpoint;
  const p256dh = json.keys?.p256dh;
  const auth = json.keys?.auth;
  if (!endpoint || !p256dh || !auth) return null;
  return { endpoint, keys: { p256dh, auth } };
}

export async function fetchVapidPublicKey(): Promise<string | null> {
  const res = await authenticatedFetch("/v1/push/vapid-public-key");
  if (!res.ok) return null;
  const body = (await res.json()) as { public_key?: string };
  return body.public_key ?? null;
}

export async function registerPushSubscription(sub: PushSubscriptionJson): Promise<boolean> {
  const res = await authenticatedFetch("/v1/push/subscriptions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub),
  });
  return res.ok;
}

export async function unregisterPushSubscription(endpoint: string): Promise<void> {
  await authenticatedFetch("/v1/push/subscriptions", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoint }),
  });
}

export async function subscribeToPushNotifications(): Promise<PushSubscription | null> {
  if (!shouldRegisterPwa()) return null;
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return null;
  if (Notification.permission !== "granted") return null;

  const publicKey = await fetchVapidPublicKey();
  if (!publicKey) return null;

  const registration = await navigator.serviceWorker.ready;
  const existing = await registration.pushManager.getSubscription();
  if (existing) {
    const json = subscriptionToJson(existing);
    if (json) await registerPushSubscription(json);
    return existing;
  }

  const sub = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(publicKey) as BufferSource,
  });
  const json = subscriptionToJson(sub);
  if (!json || !(await registerPushSubscription(json))) return null;
  return sub;
}

export async function unsubscribeFromPushNotifications(): Promise<void> {
  if (!("serviceWorker" in navigator)) return;
  const registration = await navigator.serviceWorker.ready;
  const sub = await registration.pushManager.getSubscription();
  if (!sub) return;
  const endpoint = sub.endpoint;
  await unregisterPushSubscription(endpoint);
  await sub.unsubscribe();
}