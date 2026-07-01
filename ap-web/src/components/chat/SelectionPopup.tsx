import { useCallback, useEffect, useRef, useState } from "react";
import { CornerUpLeftIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

export function SelectionPopup({
  containerRef,
  onReply,
}: {
  containerRef: React.RefObject<HTMLElement | null>;
  onReply: (text: string) => void;
}) {
  const [popupPos, setPopupPos] = useState<{ x: number; y: number } | null>(null);
  const selectedTextRef = useRef<string>("");

  const updatePopup = useCallback(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    const text = sel.toString().trim();
    if (!text) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    const container = containerRef.current;
    if (!container) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }
    const anchor = sel.anchorNode;
    if (!anchor || !container.contains(anchor)) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    const range = sel.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    setPopupPos({
      x: rect.left + rect.width / 2,
      y: rect.top,
    });
    selectedTextRef.current = text;
  }, [containerRef]);

  useEffect(() => {
    document.addEventListener("mouseup", updatePopup);
    document.addEventListener("selectionchange", updatePopup);
    return () => {
      document.removeEventListener("mouseup", updatePopup);
      document.removeEventListener("selectionchange", updatePopup);
    };
  }, [updatePopup]);

  if (!popupPos) return null;

  return (
    <div
      style={{
        position: "fixed",
        left: popupPos.x,
        top: popupPos.y,
        transform: "translate(-50%, calc(-100% - 6px))",
        zIndex: 50,
      }}
    >
      <Button
        type="button"
        variant="secondary"
        size="sm"
        className="gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110"
        onMouseDown={(e) => {
          e.preventDefault();
        }}
        onClick={() => {
          const text = selectedTextRef.current;
          if (text) {
            onReply(text);
            window.getSelection()?.removeAllRanges();
            setPopupPos(null);
            selectedTextRef.current = "";
          }
        }}
      >
        <CornerUpLeftIcon className="size-3.5" />
        Reply ↵
      </Button>
    </div>
  );
}