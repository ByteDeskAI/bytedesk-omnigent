import { useEffect, useRef, useState, type RefObject } from "react";
import { WandSparklesIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useResizableCommentsPanel } from "@/hooks/useResizableCommentsPanel";
import { getCurrentAuthorId } from "@/lib/identity";
import { cn } from "@/lib/utils";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";
import { CommentCard } from "./components/CommentCard";

export type { ActiveSelection };

export interface CommentsPanelProps {
  comments: Comment[];
  addressedComments: Comment[];
  activeSelection: ActiveSelection | null;
  onAddComment: (body: string) => void;
  onAddressAll: () => void;
  onEditComment: (id: string, body: string) => void;
  onDeleteComment: (id: string) => void;
  onClickComment: (comment: Comment) => void;
  canAddress: boolean;
  addressPending: boolean;
  canEdit?: boolean;
  onCopyCommentLink?: (commentId: string) => void;
  pendingBodyRef?: RefObject<string>;
}

type Tab = "open" | "addressed";
const TABS: Tab[] = ["open", "addressed"];

export function CommentsPanel({
  comments,
  addressedComments,
  activeSelection,
  onAddComment,
  onAddressAll,
  onEditComment,
  onDeleteComment,
  onClickComment,
  canAddress,
  addressPending,
  canEdit = true,
  pendingBodyRef,
  onCopyCommentLink,
}: CommentsPanelProps) {
  const [body, setBody] = useState("");
  const [tab, setTab] = useState<Tab>("open");
  const addCommentTextareaRef = useRef<HTMLTextAreaElement>(null);
  const { width, containerRef, isDesktop, handleProps } = useResizableCommentsPanel();
  const activeSelectionStartIndex = activeSelection?.start_index ?? null;
  const activeSelectionEndIndex = activeSelection?.end_index ?? null;

  const currentAuthorId = getCurrentAuthorId();
  const canModify = (c: Comment): boolean =>
    canEdit && (c.created_by == null || c.created_by === currentAuthorId);

  useEffect(() => {
    setBody("");
    if (pendingBodyRef) pendingBodyRef.current = "";
  }, [activeSelectionStartIndex, activeSelectionEndIndex, pendingBodyRef]);

  useEffect(() => {
    if (activeSelectionStartIndex === null || activeSelectionEndIndex === null) return;
    const isExisting = comments.some(
      (c) =>
        c.start_index === activeSelectionStartIndex && c.end_index === activeSelectionEndIndex,
    );
    if (!isExisting) {
      const id = requestAnimationFrame(() => addCommentTextareaRef.current?.focus());
      return () => cancelAnimationFrame(id);
    }
  }, [activeSelectionStartIndex, activeSelectionEndIndex, comments]);

  return (
    <div
      ref={containerRef}
      style={isDesktop && width != null ? { width } : undefined}
      className="relative flex shrink-0 flex-col overflow-hidden border-border w-full h-64 border-t md:h-auto md:border-t-0 md:border-l"
    >
      {isDesktop && (
        <div
          {...handleProps}
          className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
        />
      )}
      <div className="flex h-11 shrink-0 items-center justify-between px-3 border-b border-border">
        <span className="text-xs font-semibold">Comments</span>
        {tab === "open" && (
          <Button
            type="button"
            variant="outline"
            size="xs"
            className="rounded-full px-3 gap-1.5"
            disabled={!canAddress || comments.length === 0 || addressPending}
            onClick={onAddressAll}
          >
            <WandSparklesIcon className="size-3.5" />
            Address All
          </Button>
        )}
      </div>

      <div className="flex shrink-0 border-b border-border">
        {TABS.map((t) => {
          const count = t === "open" ? comments.length : addressedComments.length;
          return (
            <button
              key={t}
              type="button"
              className={cn(
                "flex-1 py-1.5 text-[11px] font-medium capitalize transition-colors cursor-pointer",
                tab === t
                  ? "border-b-2 border-primary text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
              onClick={() => setTab(t)}
            >
              {t === "open" ? "Open" : "Addressed"}
              {count > 0 && (
                <span className="ml-1 rounded-full bg-muted px-1.5 py-0.5 text-[10px] tabular-nums">
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {!canEdit && (
        <div className="shrink-0 border-b border-border px-3 py-2 text-xs text-muted-foreground">
          You have read-only access to this session.
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {tab === "open" &&
          activeSelection != null &&
          !comments.some(
            (c) =>
              c.start_index === activeSelection.start_index &&
              c.end_index === activeSelection.end_index,
          ) &&
          (canEdit ? (
            <div className="space-y-2 border-b border-border px-3 py-2">
              {activeSelection.anchor_content && (
                <div className="truncate rounded bg-muted/40 px-2 py-1 font-mono text-[10px] text-muted-foreground">
                  <span className="text-foreground/60">Selection: </span>
                  {activeSelection.anchor_content.trim().split("\n")[0]}
                </div>
              )}
              <textarea
                ref={addCommentTextareaRef}
                className="w-full resize-none rounded border border-border bg-background px-2 py-1.5 text-xs placeholder:text-muted-foreground"
                rows={3}
                placeholder="Add a comment…"
                value={body}
                onChange={(e) => {
                  setBody(e.target.value);
                  if (pendingBodyRef) pendingBodyRef.current = e.target.value;
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && body.trim()) {
                    e.preventDefault();
                    onAddComment(body.trim());
                    setBody("");
                    if (pendingBodyRef) pendingBodyRef.current = "";
                  }
                }}
              />
              <Button
                type="button"
                size="xs"
                className="w-full"
                disabled={!body.trim()}
                onClick={() => {
                  onAddComment(body.trim());
                  setBody("");
                  if (pendingBodyRef) pendingBodyRef.current = "";
                }}
              >
                Add Comment
              </Button>
            </div>
          ) : null)}

        {tab === "open" ? (
          comments.length === 0 ? (
            <div className="flex items-center justify-center p-8 text-xs text-muted-foreground">
              No open comments.
            </div>
          ) : (
            <div className="space-y-2 p-3">
              {comments.map((c) => (
                <CommentCard
                  key={c.id}
                  comment={c}
                  isSelected={
                    activeSelection?.start_index === c.start_index &&
                    activeSelection?.end_index === c.end_index
                  }
                  onClick={() => onClickComment(c)}
                  onDelete={canModify(c) ? () => onDeleteComment(c.id) : undefined}
                  onEdit={canModify(c) ? (newBody) => onEditComment(c.id, newBody) : undefined}
                  onCopyLink={onCopyCommentLink ? () => onCopyCommentLink(c.id) : undefined}
                />
              ))}
            </div>
          )
        ) : addressedComments.length === 0 ? (
          <div className="flex items-center justify-center p-8 text-xs text-muted-foreground">
            No addressed comments.
          </div>
        ) : (
          <div className="space-y-2 p-3">
            {addressedComments.map((c) => (
              <CommentCard
                key={c.id}
                comment={c}
                onDelete={canModify(c) ? () => onDeleteComment(c.id) : undefined}
                onCopyLink={onCopyCommentLink ? () => onCopyCommentLink(c.id) : undefined}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}