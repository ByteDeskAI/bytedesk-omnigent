import { useCallback, useEffect, useMemo, useState } from "react";
import {
  RunnerOfflineError,
  type WorkspaceChangedFile,
  type WorkspaceFile,
} from "@/hooks/useWorkspaceChangedFiles";
import { TooltipProvider } from "@/components/ui/tooltip";
import { RunnerAsleepHint } from "./RunnerAsleepHint";
import {
  buildTree,
  defaultExpandedPaths,
  expandedPathsCache,
} from "./components/folderTreeUtils";
import {
  FolderTreeNodeRow,
  FolderTreeSearchResultRow,
} from "./components/FolderTreeRows";

export function FolderTree({
  files,
  isLoading,
  isError,
  error,
  onFileSelect,
  conversationId,
  showHidden,
  onShowHidden,
  changedFiles,
  runnerWentOffline = false,
  searchQuery = "",
  searchResults,
  isSearching = false,
  isSearchError = false,
  searchError = null,
}: {
  files: WorkspaceFile[] | undefined;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  showHidden: boolean;
  onShowHidden?: () => void;
  changedFiles: WorkspaceChangedFile[] | undefined;
  runnerWentOffline?: boolean;
  searchQuery?: string;
  searchResults?: WorkspaceFile[];
  isSearching?: boolean;
  isSearchError?: boolean;
  searchError?: Error | null;
}) {
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => {
    if (!conversationId) return new Set();
    const cached = expandedPathsCache.get(conversationId);
    if (cached) return new Set(cached);
    if (files) {
      const initial = defaultExpandedPaths(files);
      expandedPathsCache.set(conversationId, initial);
      return new Set(initial);
    }
    return new Set();
  });

  useEffect(() => {
    if (!conversationId) return;
    if (!files || expandedPathsCache.has(conversationId)) return;
    const initial = defaultExpandedPaths(files);
    expandedPathsCache.set(conversationId, initial);
    setExpandedPaths(new Set(initial));
  }, [conversationId, files]);

  const changedFileMap = useMemo<Map<string, WorkspaceChangedFile["status"]>>(() => {
    if (!changedFiles) return new Map();
    return new Map(changedFiles.map((f) => [f.path, f.status]));
  }, [changedFiles]);

  const dirtyDirMap = useMemo<Map<string, WorkspaceChangedFile["status"]>>(() => {
    if (!changedFiles) return new Map();
    const STATUS_PRIORITY = { created: 3, modified: 2, deleted: 1 } as const;
    const result = new Map<string, WorkspaceChangedFile["status"]>();
    for (const file of changedFiles) {
      const parts = file.path.split("/");
      for (let i = 1; i < parts.length; i++) {
        const dirPath = parts.slice(0, i).join("/");
        const existing = result.get(dirPath);
        if (!existing || STATUS_PRIORITY[file.status] > STATUS_PRIORITY[existing]) {
          result.set(dirPath, file.status);
        }
      }
    }
    return result;
  }, [changedFiles]);

  const togglePath = useCallback(
    (path: string) => {
      setExpandedPaths((prev) => {
        const next = new Set(prev);
        if (next.has(path)) next.delete(path);
        else next.add(path);
        if (conversationId) expandedPathsCache.set(conversationId, next);
        return next;
      });
    },
    [conversationId],
  );

  if (searchQuery.trim().length > 0) {
    if (isSearching && !searchResults) {
      return <p className="px-2 py-1 text-muted-foreground text-xs">Searching…</p>;
    }
    if (isSearchError) {
      return (
        <p className="px-2 py-1 text-destructive text-xs">
          Search failed: {searchError instanceof Error ? searchError.message : "Unknown error"}
        </p>
      );
    }
    if (!searchResults || searchResults.length === 0) {
      return (
        <p className="px-2 py-1 text-muted-foreground text-xs">
          No files match "{searchQuery.trim()}"
        </p>
      );
    }
    const visibleResults = showHidden
      ? searchResults
      : searchResults.filter((f) => !f.path.split("/").some((seg) => seg.startsWith(".")));
    if (visibleResults.length === 0) {
      const hiddenCount = searchResults.length;
      return (
        <p className="px-2 py-1 text-muted-foreground text-xs">
          {hiddenCount} match{hiddenCount === 1 ? "" : "es"} in hidden directories.{" "}
          <button
            type="button"
            className="cursor-pointer underline hover:text-foreground"
            onClick={() => onShowHidden?.()}
          >
            Show hidden files
          </button>
        </p>
      );
    }
    return (
      <TooltipProvider>
        <ul className="flex flex-col gap-0.5">
          {visibleResults.map((file) => (
            <FolderTreeSearchResultRow
              key={file.path}
              file={file}
              onFileSelect={onFileSelect}
              conversationId={conversationId}
              changedFileMap={changedFileMap}
            />
          ))}
        </ul>
      </TooltipProvider>
    );
  }

  if (isLoading) {
    return <p className="px-2 py-1 text-muted-foreground text-xs">Loading…</p>;
  }
  if (isError) {
    if (error instanceof RunnerOfflineError) {
      if (runnerWentOffline) return <RunnerAsleepHint />;
      return <p className="px-2 py-1 text-muted-foreground text-xs">No files in workspace</p>;
    }
    return (
      <p className="px-2 py-1 text-destructive text-xs">
        Failed to load: {error instanceof Error ? error.message : String(error)}
      </p>
    );
  }
  if (!files || files.length === 0) {
    return <p className="px-2 py-1 text-muted-foreground text-xs">No files in workspace</p>;
  }

  const tree = buildTree(files);
  const visibleTree = showHidden ? tree : tree.filter((n) => !n.name.startsWith("."));
  if (visibleTree.length === 0) {
    return (
      <p className="px-2 py-1 text-muted-foreground text-xs">
        All files are hidden — click the eye icon to reveal them.
      </p>
    );
  }
  return (
    <TooltipProvider>
      <ul className="flex flex-col gap-0.5">
        {visibleTree.map((node) => (
          <FolderTreeNodeRow
            key={node.type === "file" ? node.file.path : node.path}
            node={node}
            depth={0}
            onFileSelect={onFileSelect}
            conversationId={conversationId}
            expandedPaths={expandedPaths}
            onTogglePath={togglePath}
            showHidden={showHidden}
            changedFileMap={changedFileMap}
            dirtyDirMap={dirtyDirMap}
          />
        ))}
      </ul>
    </TooltipProvider>
  );
}