import { lazy, Suspense, type RefObject } from "react";
import { type Comment } from "@/hooks/useComments";
import { useFileContent } from "@/hooks/useFileContent";
import { MarkdownRichTextViewer } from "../../MarkdownRichTextViewer";
import { TruncatedBanner } from "../../TruncatedBanner";
import { type ActiveSelection, type SaveStatus } from "../../codeViewerHelpers";
import { CodeViewerAddCommentButton } from "./CodeViewerAddCommentButton";
import { CodeViewerFindBar } from "./CodeViewerFindBar";
import { CodeViewerMarkdownPreview } from "./CodeViewerMarkdownPreview";
import { CodeViewerShikiSource } from "./CodeViewerShikiSource";
import type { useCodeViewerState } from "./useCodeViewerState";

const MonacoCodeEditor = lazy(() =>
  import("../../MonacoCodeEditor").then((m) => ({ default: m.MonacoCodeEditor })),
);

interface CodeViewerMainViewProps {
  conversationId: string;
  path: string;
  fileQuery: ReturnType<typeof useFileContent>;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (
    sel: { start_index: number; end_index: number; anchor_content: string } | null,
  ) => void;
  searchOpen: boolean;
  setSearchOpen: (open: boolean) => void;
  searchInputRef: RefObject<HTMLInputElement | null>;
  viewMode: "editor" | "preview" | "source" | "diff";
  onDirtyChange?: (isDirty: boolean) => void;
  onSaveStatusChange?: (status: SaveStatus) => void;
  pendingBodyRef?: RefObject<string>;
  viewerState: ReturnType<typeof useCodeViewerState>;
}

export function CodeViewerMainView({
  conversationId,
  path,
  fileQuery,
  comments,
  activeSelection,
  onSetActiveSelection,
  searchOpen,
  setSearchOpen,
  searchInputRef,
  viewMode,
  onDirtyChange,
  onSaveStatusChange,
  pendingBodyRef,
  viewerState,
}: CodeViewerMainViewProps) {
  const {
    content,
    truncated,
    lang,
    showMonaco,
    rawLines,
    tokenLines,
    codeContainerRef,
    selectionAnchor,
    setSelectionAnchor,
    searchQuery,
    setSearchQuery,
    matches,
    safeMatchIdx,
    matchLabel,
    matchLineRefs,
    setCurrentMatchIdx,
    handleSearchHandled,
  } = viewerState;

  if (viewMode === "editor" && lang === "markdown") {
    return (
      <MarkdownRichTextViewer
        content={content}
        conversationId={conversationId}
        path={path}
        isSettled={fileQuery.isSuccess}
        truncated={truncated}
        onDirtyChange={onDirtyChange}
        comments={comments}
        activeSelection={activeSelection}
        onSetActiveSelection={onSetActiveSelection}
        pendingBodyRef={pendingBodyRef}
      />
    );
  }

  if (viewMode === "preview" && (lang === "markdown" || lang === "html")) {
    const preview =
      lang === "markdown" ? (
        <CodeViewerMarkdownPreview content={content} />
      ) : (
        <iframe
          srcDoc={content}
          sandbox=""
          title="HTML preview"
          className="w-full h-full border-0"
        />
      );
    if (!truncated) return preview;
    return (
      <div className="flex h-full flex-col">
        <TruncatedBanner />
        <div className="min-h-0 flex-1">{preview}</div>
      </div>
    );
  }

  if (showMonaco) {
    return (
      <Suspense
        fallback={
          <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
            Loading…
          </div>
        }
      >
        <MonacoCodeEditor
          content={content}
          conversationId={conversationId}
          path={path}
          isSettled={fileQuery.isSuccess}
          truncated={truncated}
          onDirtyChange={onDirtyChange}
          onSaveStatusChange={onSaveStatusChange}
          searchOpen={searchOpen}
          onSearchHandled={handleSearchHandled}
          comments={comments}
          activeSelection={activeSelection}
          onSetActiveSelection={onSetActiveSelection}
          pendingBodyRef={pendingBodyRef}
        />
      </Suspense>
    );
  }

  return (
    <>
      {truncated && <TruncatedBanner />}
      {searchOpen && (
        <CodeViewerFindBar
          searchInputRef={searchInputRef}
          searchQuery={searchQuery}
          setSearchQuery={setSearchQuery}
          matchLabel={matchLabel}
          hasMatches={matches.length > 0}
          onPrev={() => setCurrentMatchIdx((i) => (i - 1 + matches.length) % matches.length)}
          onNext={() => setCurrentMatchIdx((i) => (i + 1) % matches.length)}
          onClose={() => {
            setSearchOpen(false);
            setSearchQuery("");
          }}
        />
      )}
      <CodeViewerShikiSource
        codeContainerRef={codeContainerRef}
        rawLines={rawLines}
        tokenLines={tokenLines}
        comments={comments}
        activeSelection={activeSelection}
        onSetActiveSelection={onSetActiveSelection}
        searchQuery={searchQuery}
        matches={matches}
        safeMatchIdx={safeMatchIdx}
        matchLineRefs={matchLineRefs}
      />
      {selectionAnchor && (
        <CodeViewerAddCommentButton
          anchor={selectionAnchor}
          onAdd={(sel) => {
            onSetActiveSelection(sel);
            setSelectionAnchor(null);
          }}
        />
      )}
    </>
  );
}