import { lazy, Suspense, type RefObject } from "react";
import { FileDiffIcon, Trash2Icon } from "lucide-react";
import { useSearchParams } from "@/lib/routing";
import type { useFileContent } from "@/hooks/useFileContent";
import type { useFileDiff } from "@/hooks/useFileDiff";
import {
  useAddComment,
  useDeleteComment,
  useUpdateComment,
} from "@/hooks/useComments";
import { useOptionalCommentSender } from "@/hooks/CommentSenderContext";
import { CodeViewer } from "../code-viewer/CodeViewer";
import { CommentsPanel, type ActiveSelection } from "../../CommentsPanel";
import type { SaveStatus } from "../../codeViewerHelpers";

const MonacoDiffViewer = lazy(() =>
  import("../../MonacoDiffViewer").then((m) => ({ default: m.MonacoDiffViewer })),
);

export function FileViewerContentPane({
  conversationId,
  path,
  viewMode,
  isDeletedFile,
  isDiffAvailable,
  diffQuery,
  diffLayout,
  fileQuery,
  openComments,
  addressedComments,
  activeSelection,
  pendingBodyRef,
  contentAreaRef,
  commentsOpen,
  canEdit,
  searchOpen,
  setSearchOpen,
  searchInputRef,
  onSetActiveSelection,
  copyCommentLink,
  setSearchParams,
  setIsEditorDirty,
  setSaveStatus,
}: {
  conversationId: string;
  path: string;
  viewMode: "editor" | "preview" | "source" | "diff";
  isDeletedFile: boolean;
  isDiffAvailable: boolean;
  diffQuery: ReturnType<typeof useFileDiff>;
  diffLayout: "unified" | "split";
  fileQuery: ReturnType<typeof useFileContent>;
  openComments: import("@/hooks/useComments").Comment[];
  addressedComments: import("@/hooks/useComments").Comment[];
  activeSelection: ActiveSelection | null;
  pendingBodyRef: RefObject<string>;
  contentAreaRef: RefObject<HTMLDivElement | null>;
  commentsOpen: boolean;
  canEdit: boolean;
  searchOpen: boolean;
  setSearchOpen: (open: boolean) => void;
  searchInputRef: RefObject<HTMLInputElement | null>;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  copyCommentLink: (commentId: string) => void;
  setSearchParams: ReturnType<typeof useSearchParams>[1];
  setIsEditorDirty: (dirty: boolean) => void;
  setSaveStatus: (status: SaveStatus) => void;
}) {
  const addComment = useAddComment(conversationId);
  const updateComment = useUpdateComment(conversationId);
  const deleteComment = useDeleteComment(conversationId);
  const sender = useOptionalCommentSender();

  return (
    <div className="min-h-0 flex-1 flex flex-col md:flex-row overflow-hidden">
      <div ref={contentAreaRef} className="flex-1 overflow-y-auto min-w-0">
        {isDeletedFile && viewMode !== "diff" ? (
          <div className="flex flex-col items-center justify-center gap-2 p-8 text-sm text-muted-foreground">
            <Trash2Icon className="size-5 opacity-40" />
            <span>This file has been deleted.</span>
            {isDiffAvailable && (
              <span className="text-xs">
                Click <FileDiffIcon className="inline size-3.5 align-text-bottom" /> to view its
                previous content.
              </span>
            )}
          </div>
        ) : viewMode === "diff" ? (
          !diffQuery.data ? (
            <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
              Loading diff…
            </div>
          ) : (
            <Suspense
              fallback={
                <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
                  Loading diff…
                </div>
              }
            >
              <MonacoDiffViewer
                key={path}
                before={diffQuery.data.before}
                after={diffQuery.data.after}
                path={path}
                layout={diffLayout}
                conversationId={conversationId}
                comments={openComments}
                activeSelection={activeSelection}
                onSetActiveSelection={onSetActiveSelection}
                pendingBodyRef={pendingBodyRef}
              />
            </Suspense>
          )
        ) : (
          <CodeViewer
            conversationId={conversationId}
            path={path}
            fileQuery={fileQuery}
            onDirtyChange={setIsEditorDirty}
            onSaveStatusChange={setSaveStatus}
            comments={openComments}
            activeSelection={activeSelection}
            onSetActiveSelection={onSetActiveSelection}
            pendingBodyRef={pendingBodyRef}
            panelOpen
            searchOpen={searchOpen}
            setSearchOpen={setSearchOpen}
            searchInputRef={searchInputRef}
            viewMode={viewMode}
          />
        )}
      </div>
      {commentsOpen && (
        <CommentsPanel
          comments={openComments}
          addressedComments={addressedComments}
          activeSelection={activeSelection}
          pendingBodyRef={pendingBodyRef}
          onCopyCommentLink={copyCommentLink}
          onAddComment={(body) => {
            if (activeSelection == null) return;
            addComment.mutate(
              {
                path,
                start_index: activeSelection.start_index,
                end_index: activeSelection.end_index,
                body,
                anchor_content: activeSelection.anchor_content,
              },
              { onSuccess: () => onSetActiveSelection(null) },
            );
          }}
          canAddress={canEdit && sender !== null}
          onAddressAll={() => {
            if (!sender) return;
            const ids = openComments.map((c) => c.id);
            sender.mutate({ comment_ids: ids });
            onSetActiveSelection(null);
          }}
          onClickComment={(comment) => {
            onSetActiveSelection({
              start_index: comment.start_index,
              end_index: comment.end_index,
              anchor_content: comment.anchor_content ?? "",
            });
            setSearchParams(
              (prev) => {
                const next = new URLSearchParams(prev);
                next.set("comment", comment.id);
                return next;
              },
              { replace: true },
            );
          }}
          onEditComment={(id, body) => updateComment.mutate({ commentId: id, body })}
          onDeleteComment={(id) => {
            deleteComment.mutate(id);
            const deleted = [...openComments, ...addressedComments].find((c) => c.id === id);
            if (
              deleted &&
              activeSelection?.start_index === deleted.start_index &&
              activeSelection?.end_index === deleted.end_index
            )
              onSetActiveSelection(null);
          }}
          addressPending={sender?.isPending ?? false}
          canEdit={canEdit}
        />
      )}
    </div>
  );
}