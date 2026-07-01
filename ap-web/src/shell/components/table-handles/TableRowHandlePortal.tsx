import { createPortal } from "react-dom";
import { MoreVertical } from "lucide-react";
import { cn } from "@/lib/utils";
import type { HandlePos } from "./tableHandleTypes";

export function TableRowHandlePortal({
  rowHandle,
  rowMenuOpen,
  isDraggingRef,
  wasDragRef,
  cancelHide,
  scheduleHide,
  mouseInUI,
  setColSelRect,
  setRowSelRect,
  startDrag,
  setRowMenu,
}: {
  rowHandle: HandlePos;
  rowMenuOpen: boolean;
  isDraggingRef: React.MutableRefObject<boolean>;
  wasDragRef: React.MutableRefObject<boolean>;
  cancelHide: () => void;
  scheduleHide: () => void;
  mouseInUI: React.MutableRefObject<boolean>;
  setColSelRect: React.Dispatch<React.SetStateAction<import("./tableHandleTypes").Rect | null>>;
  setRowSelRect: React.Dispatch<React.SetStateAction<import("./tableHandleTypes").Rect | null>>;
  startDrag: (
    e: React.MouseEvent,
    type: "row" | "col",
    fromIndex: number,
    tableRowIndex: number,
    sourceRect: import("./tableHandleTypes").Rect,
  ) => void;
  setRowMenu: React.Dispatch<
    React.SetStateAction<{ anchorTop: number; anchorLeft: number; handle: HandlePos } | null>
  >;
}) {
  return createPortal(
    <div
      role="button"
      tabIndex={0}
      aria-label="Row options"
      className={cn(
        "fixed z-50 flex cursor-grab items-center justify-center rounded-md",
        "border border-primary/30 bg-primary/10 text-primary shadow-sm transition-colors",
        "hover:bg-primary hover:text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
        rowMenuOpen ? "bg-primary text-primary-foreground" : "",
      )}
      style={{
        top: rowHandle.top + 2,
        left: rowHandle.left - 22,
        width: 20,
        height: rowHandle.rowHeight - 4,
      }}
      onMouseEnter={() => {
        mouseInUI.current = true;
        cancelHide();
        setColSelRect(null);
        setRowSelRect({
          top: rowHandle.top,
          left: rowHandle.left,
          width: rowHandle.rowWidth,
          height: rowHandle.rowHeight,
        });
      }}
      onMouseLeave={() => {
        mouseInUI.current = false;
        if (!isDraggingRef.current) setRowSelRect(null);
        scheduleHide();
      }}
      onMouseDown={(e) =>
        startDrag(e, "row", rowHandle.rowIndex, rowHandle.rowIndex, {
          top: rowHandle.top,
          left: rowHandle.left,
          width: rowHandle.rowWidth,
          height: rowHandle.rowHeight,
        })
      }
      onClick={(e) => {
        if (wasDragRef.current) {
          wasDragRef.current = false;
          return;
        }
        const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
        setRowMenu({ anchorTop: rect.bottom + 4, anchorLeft: rect.left, handle: rowHandle });
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
          setRowMenu({ anchorTop: rect.bottom + 4, anchorLeft: rect.left, handle: rowHandle });
        }
      }}
    >
      <MoreVertical className="size-3.5" />
    </div>,
    document.body,
  );
}