// TipTap-based rich text editor for markdown files.
//
// Comment UX:
//   The user selects any text in the editor.  A floating "Add Comment" button
//   appears above the selection (rendered by MarkdownCommentPlugin).  Clicking
//   it creates a transient "pending" ProseMirror Decoration (blue highlight)
//   so the range stays visible while the user types in the comment textarea,
//   then calls onSetActiveSelection with absolute char offsets into the raw
//   file.  Existing comments are highlighted as yellow spans via Decorations
//   that remap automatically through transactions.
//
// Key design choices:
//   • Decorations never touch the document → markdown serialisation is clean.
//   • Comment anchor mapping uses doc.textBetween("\n") for plain-text offsets,
//     avoiding markdown-syntax drift.
//   • ProseMirror positions are stable integers; binary search maps text
//     offsets to PM positions without bespoke offset-inversion code.

import { useRef } from "react";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";
import { useCanEdit } from "@/hooks/usePermissions";
import { useMarkdownEditorSync } from "./useMarkdownEditorSync";
import { type CommentDecorationState } from "./TipTapCommentExtension";
import { MarkdownRichTextViewerInner } from "./components/markdown-rich-text/MarkdownRichTextViewerInner";

interface MarkdownRichTextViewerProps {
  content: string;
  conversationId: string;
  path: string;
  isSettled: boolean;
  truncated?: boolean;
  onDirtyChange?: (isDirty: boolean) => void;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  pendingBodyRef?: React.RefObject<string>;
}

export function MarkdownRichTextViewer({
  content,
  conversationId,
  path,
  isSettled,
  truncated = false,
  onDirtyChange,
  comments,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
}: MarkdownRichTextViewerProps) {
  const canEdit = useCanEdit(conversationId) && !truncated;
  const setContentRef = useRef<((content: string) => void) | null>(null);

  const {
    editorKey,
    isDirty,
    setDirty,
    hasExternalUpdate,
    discardAndApplyExternal,
    dismissExternalUpdate,
    markSaved,
    reconcileServerContent,
  } = useMarkdownEditorSync({
    content,
    path,
    isSettled,
    onDirtyChange,
    setContentRef,
  });

  const commentStateRef = useRef<CommentDecorationState | null>(null);

  return (
    <MarkdownRichTextViewerInner
      key={`${conversationId}:${editorKey}`}
      content={content}
      conversationId={conversationId}
      path={path}
      canEdit={canEdit}
      truncated={truncated}
      isDirty={isDirty}
      setDirty={setDirty}
      hasExternalUpdate={hasExternalUpdate}
      discardAndApplyExternal={discardAndApplyExternal}
      dismissExternalUpdate={dismissExternalUpdate}
      markSaved={markSaved}
      reconcileServerContent={reconcileServerContent}
      comments={comments}
      activeSelection={activeSelection}
      onSetActiveSelection={onSetActiveSelection}
      pendingBodyRef={pendingBodyRef}
      commentStateRef={commentStateRef}
      setContentRef={setContentRef}
    />
  );
}
