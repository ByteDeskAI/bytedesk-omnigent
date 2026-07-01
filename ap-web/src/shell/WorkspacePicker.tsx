import {
  FolderIcon,
  FileIcon,
  ArrowUpIcon,
  HomeIcon,
  EyeIcon,
  EyeOffIcon,
  CheckIcon,
  XIcon,
  AlertTriangleIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import {
  basename,
  listingFilter,
  normalizeTypedPath,
  parentOf,
} from "./components/workspacePickerUtils";

export {
  parentOf,
  normalizeTypedPath,
  basename,
  isNavigablePath,
  listingFilter,
} from "./components/workspacePickerUtils";

interface WorkspacePickerProps {
  hostId: string | null;
  onSelect?: (path: string) => void;
  onNavigate?: (path: string) => void;
  onClose?: () => void;
  initialPath?: string;
  occupancyForPath?: (absolutePath: string) => number;
}

export function WorkspacePicker({
  hostId,
  onSelect,
  onClose,
  onNavigate,
  initialPath,
  occupancyForPath,
}: WorkspacePickerProps) {
  const [path, setPath] = useState<string>(initialPath ?? "");
  const [pathInput, setPathInput] = useState<string>("");
  const [resolvedHome, setResolvedHome] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const userEditedRef = useRef(false);

  const prevHostId = useRef(hostId);
  useEffect(() => {
    if (prevHostId.current === hostId) return;
    prevHostId.current = hostId;
    setPath("");
    setPathInput("");
    setResolvedHome(null);
    userEditedRef.current = false;
  }, [hostId]);

  const { data, isLoading, error, isPlaceholderData } = useHostFilesystem(hostId, path);

  useEffect(() => {
    if (
      path === "" &&
      resolvedHome === null &&
      !isPlaceholderData &&
      data &&
      data.entries.length > 0
    ) {
      const first = data.entries[0];
      const idx = first.path.lastIndexOf("/");
      if (idx > 0) {
        setResolvedHome(first.path.slice(0, idx));
      } else if (idx === 0) {
        setResolvedHome("/");
      }
    }
  }, [path, resolvedHome, data, isPlaceholderData]);

  const listedAbsolute =
    !isPlaceholderData && data && data.entries.length > 0 ? parentOf(data.entries[0].path) : null;

  const currentAbsolute = path.startsWith("/") ? path : (listedAbsolute ?? path);

  const occupiedCount =
    occupancyForPath && currentAbsolute.startsWith("/") ? occupancyForPath(currentAbsolute) : 0;

  useEffect(() => {
    if (userEditedRef.current) return;
    setPathInput(currentAbsolute);
  }, [currentAbsolute]);

  const onNavigateRef = useRef(onNavigate);
  onNavigateRef.current = onNavigate;
  useEffect(() => {
    if (currentAbsolute.startsWith("/")) {
      onNavigateRef.current?.(currentAbsolute);
    }
  }, [currentAbsolute]);

  const parent = parentOf(currentAbsolute);

  const activeFilter = listingFilter(pathInput, currentAbsolute, resolvedHome);
  const includeHidden = showHidden || (activeFilter?.startsWith(".") ?? false);

  const entries = (data?.entries ?? [])
    .filter((e) => includeHidden || !e.name.startsWith("."))
    .filter(
      (e) => activeFilter === null || e.name.toLowerCase().startsWith(activeFilter.toLowerCase()),
    )
    .sort((a, b) => {
      if (a.type === "directory" && b.type !== "directory") return -1;
      if (a.type !== "directory" && b.type === "directory") return 1;
      return a.name.localeCompare(b.name);
    });

  function navigateTo(next: string) {
    userEditedRef.current = false;
    setPath(next);
  }

  function commitPathInput() {
    const normalized = normalizeTypedPath(pathInput, resolvedHome);
    userEditedRef.current = false;
    if (normalized === null) {
      setPathInput(currentAbsolute);
      return;
    }
    if (normalized !== currentAbsolute) {
      navigateTo(normalized);
    } else {
      setPathInput(currentAbsolute);
    }
  }

  function handleSelect() {
    if (currentAbsolute === "" || currentAbsolute === null) {
      return;
    }
    onSelect?.(currentAbsolute);
  }

  return (
    <div
      className="flex max-h-80 min-h-0 flex-col rounded-md border"
      data-testid="workspace-picker"
    >
      <div className="flex shrink-0 items-center gap-1.5 border-b px-2 py-1.5">
        <button
          type="button"
          onClick={() => parent !== null && navigateTo(parent)}
          disabled={parent === null}
          aria-label="Up one level"
          title="Up one level"
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-30"
          data-testid="workspace-picker-up"
        >
          <ArrowUpIcon className="size-4" />
        </button>
        <button
          type="button"
          onClick={() => navigateTo("")}
          aria-label="Home"
          title="Home"
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          data-testid="workspace-picker-home"
        >
          <HomeIcon className="size-4" />
        </button>
        <input
          type="text"
          value={pathInput}
          onChange={(e) => {
            userEditedRef.current = true;
            setPathInput(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitPathInput();
            }
          }}
          onBlur={commitPathInput}
          placeholder="~"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          className="min-w-0 flex-1 bg-transparent text-xs text-muted-foreground focus:outline-none"
          data-testid="workspace-picker-path-input"
        />
        <button
          type="button"
          onClick={() => setShowHidden((v) => !v)}
          aria-label={showHidden ? "Hide hidden" : "Show hidden"}
          aria-pressed={showHidden}
          title={showHidden ? "Hide hidden" : "Show hidden"}
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          data-testid="workspace-picker-show-hidden"
        >
          {showHidden ? <EyeIcon className="size-4" /> : <EyeOffIcon className="size-4" />}
        </button>
        {onSelect && (
          <Button
            type="button"
            size="sm"
            disabled={currentAbsolute === "" || currentAbsolute === null}
            onClick={handleSelect}
            title={`Select this folder: ${basename(currentAbsolute)}`}
            className="shrink-0"
            data-testid="workspace-picker-select"
          >
            <CheckIcon className="size-3.5" />
            Select
          </Button>
        )}
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            title="Close"
            className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            data-testid="workspace-picker-close"
          >
            <XIcon className="size-4" />
          </button>
        )}
      </div>
      {occupiedCount > 0 && (
        <div
          className="flex shrink-0 items-start gap-1.5 border-b bg-warning/10 px-3 py-2 text-xs text-warning"
          data-testid="workspace-picker-conflict"
        >
          <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
          <span>
            {occupiedCount === 1 ? "1 other agent is" : `${occupiedCount} other agents are`} working
            in this directory. Write operations may conflict — name a git branch to work in an
            isolated copy.
          </span>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading && <div className="px-3 py-3 text-xs text-muted-foreground">Loading…</div>}
        {error !== null && error !== undefined && !isLoading && (
          <div className="px-3 py-3 text-xs text-destructive" data-testid="workspace-picker-error">
            {error instanceof Error ? error.message : "Failed to load directory"}
          </div>
        )}
        {!isLoading && error === null && entries.length === 0 && (
          <div className="px-3 py-3 text-xs text-muted-foreground">
            {activeFilter !== null ? "No matching entries" : "(empty directory)"}
          </div>
        )}
        {entries.map((entry) => {
          const isDir = entry.type === "directory";
          return (
            <button
              key={entry.path}
              type="button"
              disabled={!isDir}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => isDir && navigateTo(entry.path)}
              className={
                "flex w-full items-center gap-2 border-b px-3 py-2 text-left text-xs last:border-b-0 " +
                (isDir
                  ? "hover:bg-accent hover:text-accent-foreground cursor-pointer"
                  : "text-muted-foreground cursor-not-allowed")
              }
              data-testid={`workspace-picker-entry-${entry.name}`}
            >
              {isDir ? <FolderIcon className="size-4" /> : <FileIcon className="size-4" />}
              <span className="flex-1 truncate">{entry.name}</span>
            </button>
          );
        })}
        {data?.truncated && (
          <div
            className="px-3 py-2 text-xs text-muted-foreground"
            data-testid="workspace-picker-truncated"
          >
            Too many entries to list fully — type a path above to jump directly.
          </div>
        )}
      </div>
    </div>
  );
}