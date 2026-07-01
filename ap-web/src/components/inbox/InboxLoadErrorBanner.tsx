import { AlertTriangleIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

export function InboxLoadErrorBanner({
  failedSessionCount,
  onRetry,
}: {
  failedSessionCount: number;
  onRetry: () => void;
}) {
  return (
    <div
      data-testid="inbox-load-error"
      className="mb-4 flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm"
    >
      <AlertTriangleIcon className="size-4 shrink-0 text-destructive" />
      <span className="flex-1">
        Couldn’t load inbox items from {failedSessionCount}{" "}
        {failedSessionCount === 1 ? "session" : "sessions"}.
      </span>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}