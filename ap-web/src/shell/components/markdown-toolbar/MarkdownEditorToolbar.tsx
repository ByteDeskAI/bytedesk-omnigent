import type { Editor } from "@tiptap/react";
import { MarkdownToolbarFormatSection } from "./MarkdownToolbarFormatSection";
import { MarkdownToolbarSaveSection } from "./MarkdownToolbarSaveSection";
export { ToolbarBtn, Divider } from "./MarkdownToolbarPrimitives";

export function ToolbarPlugin({
  editor,
  onSave,
  isSaving,
  isDirty,
  saveError,
  saveDisabled,
  hasExternalUpdate,
}: {
  editor: Editor | null;
  onSave: (markdown: string) => void;
  isSaving: boolean;
  isDirty: boolean;
  saveError: boolean;
  saveDisabled: boolean;
  hasExternalUpdate: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-0.5 border-b border-border bg-card px-2 py-1 shrink-0">
      <MarkdownToolbarFormatSection editor={editor} />
      <MarkdownToolbarSaveSection
        editor={editor}
        onSave={onSave}
        isSaving={isSaving}
        isDirty={isDirty}
        saveError={saveError}
        saveDisabled={saveDisabled}
        hasExternalUpdate={hasExternalUpdate}
      />
    </div>
  );
}