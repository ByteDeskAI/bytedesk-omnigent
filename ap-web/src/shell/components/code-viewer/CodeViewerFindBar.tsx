import { ChevronDownIcon, ChevronUpIcon, SearchIcon, XIcon } from "lucide-react";
import type { RefObject } from "react";

export function CodeViewerFindBar({
  searchInputRef,
  searchQuery,
  setSearchQuery,
  matchLabel,
  hasMatches,
  onPrev,
  onNext,
  onClose,
}: {
  searchInputRef: RefObject<HTMLInputElement | null>;
  searchQuery: string;
  setSearchQuery: (q: string) => void;
  matchLabel: string;
  hasMatches: boolean;
  onPrev: () => void;
  onNext: () => void;
  onClose: () => void;
}) {
  return (
    <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-border bg-card/90 px-3 py-1.5 backdrop-blur">
      <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
      <input
        ref={searchInputRef}
        type="text"
        value={searchQuery}
        onChange={(e) => setSearchQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && hasMatches) {
            e.preventDefault();
            if (e.shiftKey) onPrev();
            else onNext();
          }
        }}
        placeholder="Find…"
        className="min-w-0 flex-1 bg-transparent text-xs outline-none"
      />
      <span className="shrink-0 text-xs text-muted-foreground">{matchLabel}</span>
      <button
        type="button"
        aria-label="Previous match"
        className="rounded p-0.5 text-muted-foreground hover:bg-muted disabled:opacity-40"
        disabled={!hasMatches}
        onClick={onPrev}
      >
        <ChevronUpIcon className="size-3.5" />
      </button>
      <button
        type="button"
        aria-label="Next match"
        className="rounded p-0.5 text-muted-foreground hover:bg-muted disabled:opacity-40"
        disabled={!hasMatches}
        onClick={onNext}
      >
        <ChevronDownIcon className="size-3.5" />
      </button>
      <button
        type="button"
        aria-label="Close search"
        className="rounded p-0.5 text-muted-foreground hover:bg-muted"
        onClick={onClose}
      >
        <XIcon className="size-3.5" />
      </button>
    </div>
  );
}