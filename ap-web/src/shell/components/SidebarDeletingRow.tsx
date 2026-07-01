import { AlertTriangleIcon, Loader2Icon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * Status row shown in place of a conversation while its delete is in
 * flight (`isError === false`) or after it failed (`isError === true`).
 * Keeps the user un-blocked: the delete dialog closes immediately and
 * this surfaces progress / failure inline in the sidebar.
 */
export function SidebarDeletingRow({
  label,
  isError,
  onRetry,
  onDismiss,
}: {
  label: string;
  isError: boolean;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  if (isError) {
    return (
      <div
        className="flex w-full items-center gap-1.5 rounded-md px-2 py-2 text-sm"
        data-testid="conversation-delete-failed"
        role="alert"
      >
        <AlertTriangleIcon className="size-3.5 shrink-0 text-destructive" />
        <span
          className="min-w-0 flex-1 truncate text-destructive"
          title={`Couldn't delete ${label}`}
        >
          Couldn&apos;t delete <span className="font-medium">{label}</span>
        </span>
        <Button type="button" variant="ghost" size="sm" className="h-6 px-1.5" onClick={onRetry}>
          Retry
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Dismiss delete error"
          onClick={onDismiss}
        >
          <XIcon className="size-3.5" />
        </Button>
      </div>
    );
  }
  return (
    <div
      className="flex w-full items-center gap-1.5 rounded-md px-2 py-2 text-sm text-muted-foreground opacity-70"
      data-testid="conversation-deleting"
      aria-live="polite"
    >
      <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
      <span className="min-w-0 flex-1 truncate" title={label}>
        {label}
      </span>
      <span className="shrink-0 text-xs">Deleting…</span>
    </div>
  );
}