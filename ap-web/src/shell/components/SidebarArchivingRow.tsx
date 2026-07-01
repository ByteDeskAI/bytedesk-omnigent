import { Loader2Icon } from "lucide-react";

/**
 * In-flight status row shown while a session is being archived (the
 * stop→archive sequence in ConversationRow.runArchive). Mirrors the
 * non-error arm of SidebarDeletingRow; archive failures fall back to
 * the interactive row rather than a persistent error state, so there's
 * no retry/dismiss affordance here.
 */
export function SidebarArchivingRow({ label }: { label: string }) {
  return (
    <div
      className="flex w-full items-center gap-1.5 rounded-md px-2 py-2 text-sm text-muted-foreground opacity-70"
      data-testid="conversation-archiving"
      aria-live="polite"
    >
      <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
      <span className="min-w-0 flex-1 truncate" title={label}>
        {label}
      </span>
      <span className="shrink-0 text-xs">Archiving…</span>
    </div>
  );
}