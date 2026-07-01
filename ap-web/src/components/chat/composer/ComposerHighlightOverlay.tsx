import { type RefObject } from "react";
import { splitSlashCommand } from "../chat-utils";

export function ComposerHighlightOverlay({
  value,
  backdropRef,
}: {
  value: string;
  backdropRef: RefObject<HTMLDivElement | null>;
}) {
  return (
    <div
      ref={backdropRef}
      aria-hidden
      data-testid="composer-highlight-overlay"
      className="pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words px-4 pt-3 pb-2 text-sm text-foreground"
    >
      {(() => {
        const split = splitSlashCommand(value);
        if (!split) return value;
        return (
          <>
            {split.before}
            <span className="text-brand-accent">{split.token}</span>
            {split.after}
          </>
        );
      })()}
    </div>
  );
}