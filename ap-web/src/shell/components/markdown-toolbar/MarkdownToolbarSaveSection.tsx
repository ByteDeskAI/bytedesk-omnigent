import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import type { Editor } from "@tiptap/react";
import "@tiptap/markdown";
import { cn } from "@/lib/utils";
import { ToolbarBtn } from "./MarkdownToolbarPrimitives";

export function MarkdownToolbarSaveSection({
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
  const [isCopied, setIsCopied] = useState(false);
  const copyTimeoutRef = useRef<number>(0);

  const getMarkdown = useCallback(() => editor?.getMarkdown() ?? "", [editor]);

  const handleCopy = useCallback(() => {
    const md = getMarkdown();
    if (!navigator?.clipboard?.writeText) return;
    navigator.clipboard
      .writeText(md)
      .then(() => {
        setIsCopied(true);
        window.clearTimeout(copyTimeoutRef.current);
        copyTimeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
      })
      .catch(() => {
        // ignore clipboard errors
      });
  }, [getMarkdown]);

  const handleSave = useCallback(() => {
    if (!isDirty || saveDisabled || hasExternalUpdate) return;
    onSave(getMarkdown());
  }, [getMarkdown, onSave, isDirty, saveDisabled, hasExternalUpdate]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        handleSave();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [handleSave]);

  const saveStatus = saveDisabled
    ? {
        label: "Offline",
        title: "Runner offline — your changes will save when it reconnects",
        tone: "offline" as const,
      }
    : saveError && isDirty
      ? { label: "Retry", title: "Save failed — click to retry", tone: "error" as const }
      : isSaving
        ? { label: "Saving…", title: "Saving…", tone: "pending" as const }
        : isDirty
          ? {
              label: "Unsaved",
              title: "Unsaved changes — ⌘S to save now",
              tone: "pending" as const,
            }
          : { label: "Saved", title: "All changes saved", tone: "saved" as const };
  const saveClickable = !saveDisabled && !hasExternalUpdate && isDirty;

  return (
    <div className="ml-auto flex items-center gap-2">
      <ToolbarBtn title="Copy" onClick={handleCopy}>
        {isCopied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      </ToolbarBtn>
      <button
        type="button"
        title={saveStatus.title}
        aria-label={saveStatus.title}
        onMouseDown={(e) => e.preventDefault()}
        onClick={saveClickable ? handleSave : undefined}
        disabled={!saveClickable}
        className={cn(
          "flex items-center gap-1 rounded px-2 py-0.5 text-xs transition-colors",
          saveStatus.tone === "error" && "text-destructive hover:bg-destructive/10 cursor-pointer",
          saveStatus.tone === "offline" && "text-warning cursor-default",
          saveStatus.tone === "pending" &&
            (saveClickable
              ? "text-muted-foreground hover:bg-muted hover:text-foreground cursor-pointer"
              : "text-muted-foreground cursor-default"),
          saveStatus.tone === "saved" && "text-muted-foreground cursor-default",
        )}
      >
        {saveStatus.tone === "saved" && <Check className="size-3.5" />}
        {saveStatus.label}
      </button>
    </div>
  );
}