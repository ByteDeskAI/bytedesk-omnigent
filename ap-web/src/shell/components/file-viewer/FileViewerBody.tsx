import { cn } from "@/lib/utils";
import { FileViewerContentPane } from "./FileViewerContentPane";
import { FileViewerToolbar } from "./FileViewerToolbar";
import { FileViewerUnsavedDialog } from "./FileViewerUnsavedDialog";
import { useFileViewerBodyState } from "./useFileViewerBodyState";

interface FileViewerProps {
  open: boolean;
  conversationId: string;
  path: string;
  onClose: () => void;
  onNavigateTo?: (path: string) => void;
  permissionLevel?: number | null;
  frameless?: boolean;
  onCommentsOpenChange?: (open: boolean) => void;
  sort?: import("../../FlatFileList").ChangedSort;
}

export function FileViewerBody({
  open,
  conversationId,
  path,
  onClose,
  onNavigateTo,
  permissionLevel,
  frameless,
  onCommentsOpenChange,
  sort = "recent",
}: FileViewerProps) {
  const state = useFileViewerBodyState({
    open,
    conversationId,
    path,
    onNavigateTo,
    permissionLevel,
    frameless,
    onCommentsOpenChange,
    sort,
  });

  const innerContent = (
    <>
      <FileViewerToolbar
        path={path}
        frameless={frameless}
        saveStatus={state.saveStatus}
        showNavButtons={state.showNavButtons}
        prevPath={state.prevPath}
        nextPath={state.nextPath}
        currentNavIdx={state.currentNavIdx}
        navigableFilesLength={state.navigableFiles.length}
        toolbarActions={state.toolbarActions}
        onClose={onClose}
        onNavigateTo={onNavigateTo}
        guardDirty={state.guardDirty}
      />
      <FileViewerContentPane
        conversationId={conversationId}
        path={path}
        viewMode={state.viewMode}
        isDeletedFile={state.isDeletedFile}
        isDiffAvailable={state.isDiffAvailable}
        diffQuery={state.diffQuery}
        diffLayout={state.diffLayout}
        fileQuery={state.fileQuery}
        openComments={state.openComments}
        addressedComments={state.addressedComments}
        activeSelection={state.activeSelection}
        pendingBodyRef={state.pendingBodyRef}
        contentAreaRef={state.contentAreaRef}
        commentsOpen={state.commentsOpen}
        canEdit={state.canEdit}
        searchOpen={state.searchOpen}
        setSearchOpen={state.setSearchOpen}
        searchInputRef={state.searchInputRef}
        onSetActiveSelection={state.handleSetActiveSelection}
        copyCommentLink={state.copyCommentLink}
        setSearchParams={state.setSearchParams}
        setIsEditorDirty={state.setIsEditorDirty}
        setSaveStatus={state.setSaveStatus}
      />
      <FileViewerUnsavedDialog
        open={state.pendingAction !== null}
        onKeepEditing={() => state.setPendingAction(null)}
        onDiscard={() => {
          state.setIsEditorDirty(false);
          state.pendingAction?.();
          state.setPendingAction(null);
        }}
      />
    </>
  );

  if (frameless) {
    return (
      <div
        data-testid="file-viewer"
        className="flex flex-col flex-1 min-h-0 overflow-hidden bg-card"
      >
        {innerContent}
      </div>
    );
  }

  return (
    <aside
      data-testid="file-viewer"
      style={{ width: state.panelWidth }}
      className={cn(
        "flex flex-col overflow-hidden bg-card transition-[translate,border-color,border-width] duration-150 ease-out",
        "fixed inset-0 z-50 shadow-lg",
        open ? "translate-x-0" : "translate-x-full",
        "md:relative md:inset-auto md:z-auto md:shadow-none md:translate-x-0 md:shrink-0",
        open ? "md:border-border md:border-l" : "md:w-0 md:border-l-0",
      )}
      aria-hidden={!open}
      data-collapsed={!open || undefined}
      inert={!open}
    >
      {state.isDesktop && (
        <div
          {...state.handleProps}
          className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
        />
      )}
      {open && innerContent}
    </aside>
  );
}