import { createPortal } from "react-dom";
import { MessageSquarePlusIcon } from "lucide-react";
import { getEmbedRoot } from "@/lib/host";

export function CodeViewerAddCommentButton({
  anchor,
  onAdd,
}: {
  anchor: {
    x: number;
    y: number;
    start_index: number;
    end_index: number;
    anchor_content: string;
  };
  onAdd: (sel: { start_index: number; end_index: number; anchor_content: string }) => void;
}) {
  return createPortal(
    <button
      data-add-comment-btn
      type="button"
      className="fixed z-50 flex items-center gap-1.5 rounded-md border border-border bg-popover backdrop-blur-xl backdrop-saturate-150 px-2.5 py-1 text-xs font-medium text-foreground shadow-md hover:bg-secondary transition-colors"
      style={{
        left: anchor.x,
        top: anchor.y,
        transform: "translateY(-100%)",
      }}
      onClick={() => {
        onAdd({
          start_index: anchor.start_index,
          end_index: anchor.end_index,
          anchor_content: anchor.anchor_content,
        });
        window.getSelection()?.removeAllRanges();
      }}
    >
      <MessageSquarePlusIcon className="size-3.5" />
      Add comment
    </button>,
    getEmbedRoot() ?? document.body,
  );
}