import { useEffect, useRef, useState } from "react";
import { FolderIcon } from "lucide-react";

import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { isNavigablePath } from "./WorkspacePicker";
import { WorkspacePathDropdown } from "./components/workspace-path-field/WorkspacePathDropdown";
import {
  MATCH_DISPLAY_LIMIT,
  splitTypedPath,
} from "./components/workspace-path-field/workspacePathFieldUtils";

export { splitTypedPath } from "./components/workspace-path-field/workspacePathFieldUtils";

interface WorkspacePathFieldProps {
  hostId: string | null;
  value: string;
  onChange: (value: string) => void;
  onBrowse: () => void;
  onCommit?: (path: string) => void;
  recent: string[];
  dropdownDisabled?: boolean;
}

export function WorkspacePathField({
  hostId,
  value,
  onChange,
  onBrowse,
  onCommit,
  recent,
  dropdownDisabled = false,
}: WorkspacePathFieldProps) {
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  const containerRef = useRef<HTMLDivElement>(null);

  const dropdownOpen = open && !dropdownDisabled;
  const trimmed = value.trim();
  const { dir, partial } = splitTypedPath(value);

  const { data, isLoading } = useHostFilesystem(hostId, dropdownOpen ? dir : null);

  const recentFilter = trimmed === "~" ? "" : trimmed;
  const filteredRecent =
    recentFilter === ""
      ? recent
      : recent.filter((p) => p.toLowerCase().includes(recentFilter.toLowerCase()));
  const recentSet = new Set(filteredRecent);

  const lowerPartial = partial.toLowerCase();
  const showHidden = partial.startsWith(".");
  const allMatches = (data?.entries ?? [])
    .filter(
      (e) =>
        e.type === "directory" &&
        e.name.toLowerCase().startsWith(lowerPartial) &&
        (showHidden || !e.name.startsWith(".")) &&
        !recentSet.has(e.path),
    )
    .map((e) => e.path);
  const matches = allMatches.slice(0, MATCH_DISPLAY_LIMIT);
  const hiddenMatchCount = allMatches.length - matches.length;

  const items = [...filteredRecent, ...matches];
  const showLoading = dropdownOpen && isLoading && matches.length === 0;
  const hasContent = filteredRecent.length > 0 || matches.length > 0 || showLoading;

  const activeDescendantId =
    highlight < 0
      ? undefined
      : highlight < filteredRecent.length
        ? `workspace-recent-${highlight}`
        : `workspace-match-${highlight - filteredRecent.length}`;

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
        setHighlight(-1);
      }
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  useEffect(() => {
    if (highlight < 0 || !containerRef.current) return;
    const el = containerRef.current.querySelector('[data-active="true"]');
    el?.scrollIntoView({ block: "nearest" });
  }, [highlight]);

  function select(path: string) {
    onChange(path);
    setOpen(false);
    setHighlight(-1);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      if (items.length > 0) {
        setHighlight((h) => Math.min(h + 1, items.length - 1));
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, -1));
    } else if (e.key === "Enter") {
      if (open && highlight >= 0 && highlight < items.length) {
        e.preventDefault();
        select(items[highlight]);
      } else if (trimmed !== "") {
        e.preventDefault();
        setOpen(false);
        setHighlight(-1);
        if (isNavigablePath(trimmed)) {
          onCommit?.(trimmed);
        }
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
        setHighlight(-1);
      }
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            setOpen(true);
            setHighlight(-1);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          placeholder="/Users/you/projects/app"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          role="combobox"
          aria-label="Working directory path"
          aria-autocomplete="list"
          aria-expanded={dropdownOpen}
          aria-controls="workspace-path-listbox"
          aria-activedescendant={dropdownOpen ? activeDescendantId : undefined}
          className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
          data-testid="workspace-path-input"
        />
        <button
          type="button"
          onClick={onBrowse}
          aria-label="Browse directories"
          className="flex size-9 shrink-0 items-center justify-center rounded-md border border-input bg-background text-muted-foreground transition hover:bg-muted hover:text-foreground"
          data-testid="workspace-browse-toggle"
        >
          <FolderIcon className="size-4" />
        </button>
      </div>

      {dropdownOpen && hasContent && (
        <WorkspacePathDropdown
          filteredRecent={filteredRecent}
          matches={matches}
          hiddenMatchCount={hiddenMatchCount}
          highlight={highlight}
          showLoading={showLoading}
          onSelect={select}
        />
      )}
    </div>
  );
}