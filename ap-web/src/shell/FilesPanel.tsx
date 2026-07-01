import { useEffect, useState } from "react";
import { useParams } from "@/lib/routing";
import { useChatStore } from "@/store/chatStore";
import {
  useWorkspaceChangedFiles,
  useWorkspaceAllFiles,
  useWorkspaceEnvironment,
  useWorkspaceFileSearch,
} from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import { type ChangedSort, FlatFileList } from "./FlatFileList";
import { FolderTree } from "./FolderTree";
import { FilesPanelHeader } from "./components/files-panel/FilesPanelHeader";
import { FilesPanelSearchSection } from "./components/files-panel/FilesPanelSearchSection";

interface FilesPanelProps {
  onFileSelect: (path: string) => void;
  flatView: boolean;
  onFlatViewChange: (flatView: boolean) => void;
  showHidden: boolean;
  onShowHiddenChange: (showHidden: boolean) => void;
  sort: ChangedSort;
  onSortChange: (sort: ChangedSort) => void;
  onClose?: () => void;
  frameless?: boolean;
}

export function FilesPanel({
  onFileSelect,
  flatView,
  onFlatViewChange,
  showHidden,
  onShowHiddenChange,
  sort: changedSort,
  onSortChange,
  onClose,
  frameless,
}: FilesPanelProps) {
  const { conversationId } = useParams<{ conversationId: string }>();
  const runnerWentOffline = useChatStore(
    (s) => s.conversationId === conversationId && s.sessionStatus === "failed",
  );
  const [collapsed, setCollapsed] = useState(false);
  const [changedSearch, setChangedSearch] = useState("");
  const [treeSearch, setTreeSearch] = useState("");
  const [debouncedTreeSearch, setDebouncedTreeSearch] = useState("");
  const [treeInclude, setTreeInclude] = useState("");
  const [debouncedTreeInclude, setDebouncedTreeInclude] = useState("");
  const [treeExclude, setTreeExclude] = useState("");
  const [debouncedTreeExclude, setDebouncedTreeExclude] = useState("");
  const [showSearchFilters, setShowSearchFilters] = useState(false);
  const fullScreen = onClose !== undefined || frameless === true;
  const contentVisible = !collapsed || fullScreen;
  const changedQuery = useWorkspaceChangedFiles(conversationId, {
    enabled: contentVisible,
  });
  const allFilesQuery = useWorkspaceAllFiles(conversationId, {
    enabled: contentVisible && !flatView,
  });
  const envQuery = useWorkspaceEnvironment(conversationId, {
    enabled: contentVisible,
  });
  const workingDir = envQuery.data?.root ?? null;
  const changedCount = changedQuery.data?.data.length ?? 0;
  const hiddenFilesCount = (changedQuery.data?.data ?? []).filter((f) =>
    f.path.split("/").some((seg) => seg.startsWith(".")),
  ).length;

  useEffect(() => {
    if (!flatView) setChangedSearch("");
    if (flatView) {
      setTreeSearch("");
      setDebouncedTreeSearch("");
      setTreeInclude("");
      setDebouncedTreeInclude("");
      setTreeExclude("");
      setDebouncedTreeExclude("");
    }
  }, [flatView]);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedTreeSearch(treeSearch);
      setDebouncedTreeInclude(treeInclude);
      setDebouncedTreeExclude(treeExclude);
    }, 300);
    return () => clearTimeout(timer);
  }, [treeSearch, treeInclude, treeExclude]);

  const treeSearchQuery = useWorkspaceFileSearch(
    conversationId,
    debouncedTreeSearch,
    debouncedTreeInclude,
    debouncedTreeExclude,
    {
      enabled: contentVisible && !flatView && debouncedTreeSearch.trim().length > 0,
    },
  );
  const treeFiltersActive = treeInclude.trim().length > 0 || treeExclude.trim().length > 0;

  return (
    <div
      className={cn(
        "@container/filespanel overflow-hidden bg-card",
        fullScreen ? "flex h-full min-h-0 flex-col" : "flex min-h-0 flex-col",
      )}
    >
      <FilesPanelHeader
        fullScreen={fullScreen}
        collapsed={collapsed}
        workingDir={workingDir}
        showHidden={showHidden}
        hiddenFilesCount={hiddenFilesCount}
        onToggleCollapsed={() => setCollapsed((v) => !v)}
        onToggleHidden={() => onShowHiddenChange(!showHidden)}
        onClose={onClose}
      />
      {contentVisible && (
        <>
          <div className="shrink-0 border-t border-border" />
          <FilesPanelSearchSection
            flatView={flatView}
            changedCount={changedCount}
            changedSort={changedSort}
            changedSearch={changedSearch}
            treeSearch={treeSearch}
            treeInclude={treeInclude}
            treeExclude={treeExclude}
            showSearchFilters={showSearchFilters}
            treeFiltersActive={treeFiltersActive}
            onFlatViewChange={onFlatViewChange}
            onSortChange={onSortChange}
            onChangedSearchChange={setChangedSearch}
            onTreeSearchChange={setTreeSearch}
            onTreeIncludeChange={setTreeInclude}
            onTreeExcludeChange={setTreeExclude}
            onToggleSearchFilters={() => setShowSearchFilters((v) => !v)}
          />
          <section
            className={cn(
              "overflow-y-auto px-2 pb-2",
              flatView ? "pt-1" : "pt-2",
              fullScreen ? "min-h-0 flex-1" : "max-h-72",
            )}
          >
            {flatView ? (
              <FlatFileList
                files={changedQuery.data?.data}
                isLoading={changedQuery.isLoading}
                isError={changedQuery.isError}
                error={changedQuery.error}
                onFileSelect={onFileSelect}
                showHidden={showHidden}
                onShowHidden={() => onShowHiddenChange(true)}
                searchQuery={changedSearch}
                sort={changedSort}
                conversationId={conversationId}
                runnerWentOffline={runnerWentOffline}
              />
            ) : (
              <FolderTree
                files={allFilesQuery.data?.data}
                isLoading={allFilesQuery.isLoading}
                isError={allFilesQuery.isError}
                error={allFilesQuery.error}
                onFileSelect={onFileSelect}
                conversationId={conversationId}
                showHidden={showHidden}
                onShowHidden={() => onShowHiddenChange(true)}
                changedFiles={changedQuery.data?.data}
                runnerWentOffline={runnerWentOffline}
                searchQuery={debouncedTreeSearch}
                searchResults={treeSearchQuery.data}
                isSearching={treeSearchQuery.isFetching}
                isSearchError={treeSearchQuery.isError}
                searchError={treeSearchQuery.error instanceof Error ? treeSearchQuery.error : null}
              />
            )}
          </section>
        </>
      )}
    </div>
  );
}