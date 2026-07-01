// Monaco-based viewer/editor for non-markdown files in the file viewer.
//
// One component serves both modes, switched by permission:
//   • read-only (no edit permission) → Monaco with readOnly:true; selection,
//     highlighting, and comment decorations still work.
//   • editable (edit permission)     → save via Cmd/Ctrl+S or the Save button.
//
// Highlighting comes from Shiki via @shikijs/monaco (github-light/dark), so
// colors match the read-only Shiki views and chat code blocks. The structure
// mirrors MarkdownRichTextViewer: an outer component owns the sync hook and
// remount key; the inner component owns the live editor instance.
//
// The comment layer (inline highlights, the floating "Add comment" button,
// click-to-navigate, reveal-on-select) lives in useMonacoCommentLayer, shared
// with the diff view. Adding a comment is gated on `canEdit && !isDirty`
// (offsets must match the saved server content).

import { useRef } from "react";
import type { Comment } from "@/hooks/useComments";
import { useCanEdit } from "@/hooks/usePermissions";
import { type ActiveSelection, type SaveStatus } from "./codeViewerHelpers";
import { useMarkdownEditorSync } from "./useMarkdownEditorSync";
import { MonacoCodeEditorInner } from "./components/monaco-code-editor/MonacoCodeEditorInner";

interface CommentProps {
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  pendingBodyRef?: React.RefObject<string>;
}

interface MonacoCodeEditorProps extends CommentProps {
  content: string;
  conversationId: string;
  path: string;
  isSettled: boolean;
  truncated?: boolean;
  onDirtyChange?: (isDirty: boolean) => void;
  onSaveStatusChange?: (status: SaveStatus) => void;
  searchOpen?: boolean;
  onSearchHandled?: () => void;
}

export function MonacoCodeEditor({
  content,
  conversationId,
  path,
  isSettled,
  truncated = false,
  onDirtyChange,
  onSaveStatusChange,
  searchOpen,
  onSearchHandled,
  comments,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
}: MonacoCodeEditorProps) {
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
  } = useMarkdownEditorSync({ content, path, isSettled, onDirtyChange, setContentRef });

  return (
    <MonacoCodeEditorInner
      key={editorKey}
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
      setContentRef={setContentRef}
      onSaveStatusChange={onSaveStatusChange}
      searchOpen={searchOpen}
      onSearchHandled={onSearchHandled}
      comments={comments}
      activeSelection={activeSelection}
      onSetActiveSelection={onSetActiveSelection}
      pendingBodyRef={pendingBodyRef}
    />
  );
}
