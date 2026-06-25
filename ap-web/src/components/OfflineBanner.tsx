import { useOffline } from "@/hooks/useOffline";

export function OfflineBanner() {
  const offline = useOffline();
  if (!offline) return null;
  return (
    <div
      role="status"
      className="bg-warning/15 text-warning border-b border-warning/30 px-4 py-2 text-center text-sm"
      data-testid="offline-banner"
    >
      You are offline. Reconnect to send messages and use terminals.
    </div>
  );
}