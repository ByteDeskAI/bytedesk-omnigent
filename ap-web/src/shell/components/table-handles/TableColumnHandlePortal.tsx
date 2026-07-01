import { createPortal } from "react-dom";
import { MoreHorizontal } from "lucide-react";
import { cn } from "@/lib/utils";
import type { HandlePos } from "./tableHandleTypes";

export function TableColumnHandlePortal({
  colHandle,
  colMenuOpen,
  isDraggingRef,
  wasDragRef,
  cancelHide,
  scheduleHide,
  mouseInUI,
  setRowSelRect,
  setColSelRect,
  startDrag,
  setColMenu,
}: {
  colHandle: HandlePos;
  colMenuOpen: boolean;
  isDraggingRef: React.MutableRefObject<boolean>;
  wasDragRef: React.MutableRefObject<boolean>;
  cancelHide: () => void;
  scheduleHide: () => void;
  mouseInUI: React.MutableRefObject<boolean>;
  setRowSelRect: React.Dispatch<React.SetStateAction<import("./tableHandleTypes").Rect | null>>;
  setColSelRect: React.Dispatch<React.SetStateAction<import("./tableHandleTypes").Rect | null>>;
  startDrag: (
    e: React.MouseEvent,
    type: "row" | "col",
    fromIndex: number,
    tableRowIndex: number,
    sourceRect: import("./tableHandleTypes").Rect,
  ) => void;
  setColMenu: React.Dispatch<
    React.SetStateAction<{ anchorTop: number; anchorLeft: number; handle: HandlePos } | null>
  >;
}) {
  return createPortal(
    <div
      role="button"
      tabIndex={0}
      aria-label="Column options"
      className={cn(
        "fixed z-50 flex cursor-grab items-center justify-center rounded-md",
        "border border-primary/30 bg-primary/10 text-primary shadow-sm transition-colors",
        "hover:bg-primary hover:text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
        colMenuOpen ? "bg-primary text-primary-foreground" : "",
      )}
      style={{
        top: colHandle.top - 24,
        left: colHandle.left + 2,
        width: colHandle.cellWidth - 4,
        height: 18,
      }}
      onMouseEnter={() => {
        mouseInUI.current = true;
        cancelHide();
        setRowSelRect(null);
        setColSelRect({
          top: colHandle.top,
          left: colHandle.left,
          width: colHandle.cellWidth,
          height: colHandle.tableHeight,
        });
      }}
      onMouseLeave={() => {
        mouseInUI.current = false;
        if (!isDraggingRef.current) setColSelRect(null);
        scheduleHide();
      }}
      onMouseDown={(e) =>
        startDrag(e, "col", colHandle.colIndex, colHandle.rowIndex, {
          top: colHandle.top,
          left: colHandle.left,
          width: colHandle.cellWidth,
          height: colHandle.tableHeight,
        })
      }
      onClick={(e) => {
        if (wasDragRef.current) {
          wasDragRef.current = false;
          return;
        }
        const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
        setColMenu({ anchorTop: rect.bottom + 4, anchorLeft: rect.left, handle: colHandle });
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
          setColMenu({ anchorTop: rect.bottom + 4, anchorLeft: rect.left, handle: colHandle });
        }
      }}
    >
      <MoreHorizontal className="size-3.5" />
    </div>,
    document.body,
  );
}