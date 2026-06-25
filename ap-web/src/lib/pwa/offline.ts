/** Whether network-dependent actions (composer send, terminal attach) should be blocked. */
export function shouldDisableNetworkActions(isOnline: boolean): boolean {
  return !isOnline;
}

export function readNavigatorOnline(): boolean {
  if (typeof navigator === "undefined") return true;
  return navigator.onLine;
}

/** Subscribe to browser online/offline events; returns cleanup. */
export function subscribeOnlineStatus(onChange: (online: boolean) => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  const handleOnline = () => onChange(true);
  const handleOffline = () => onChange(false);
  window.addEventListener("online", handleOnline);
  window.addEventListener("offline", handleOffline);
  return () => {
    window.removeEventListener("online", handleOnline);
    window.removeEventListener("offline", handleOffline);
  };
}