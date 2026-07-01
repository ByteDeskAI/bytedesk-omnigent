import { useCallback } from "react";
import type { Editor } from "@tiptap/react";
import { colIndexAtX, moveColumnToIndex, moveRowToIndex, rowIndexAtY } from "./tableBubbleMenuUtils";
import { DRAG_THRESHOLD, type Rect } from "./tableHandleTypes";

export function useTableHandleDrag(
  editor: Editor,
  {
    isDraggingRef,
    wasDragRef,
    dragCleanupRef,
    setRowSelRect,
    setColSelRect,
    setDragTargetRect,
    setDragGhostRect,
    setDropIndicator,
  }: {
    isDraggingRef: React.MutableRefObject<boolean>;
    wasDragRef: React.MutableRefObject<boolean>;
    dragCleanupRef: React.MutableRefObject<(() => void) | null>;
    setRowSelRect: React.Dispatch<React.SetStateAction<Rect | null>>;
    setColSelRect: React.Dispatch<React.SetStateAction<Rect | null>>;
    setDragTargetRect: React.Dispatch<React.SetStateAction<Rect | null>>;
    setDragGhostRect: React.Dispatch<React.SetStateAction<Rect | null>>;
    setDropIndicator: React.Dispatch<React.SetStateAction<Rect | null>>;
  },
) {
  const startDrag = useCallback(
    (
      e: React.MouseEvent,
      type: "row" | "col",
      fromIndex: number,
      tableRowIndex: number,
      sourceRect: Rect,
    ) => {
      e.preventDefault();
      wasDragRef.current = false;

      const startX = e.clientX;
      const startY = e.clientY;
      let dragActive = false;
      let dropTarget: number | null = null;
      const dom = editor.view?.dom;
      if (!dom) return;

      const onMove = (ev: MouseEvent) => {
        if (!dragActive) {
          const dx = ev.clientX - startX;
          const dy = ev.clientY - startY;
          if (Math.sqrt(dx * dx + dy * dy) < DRAG_THRESHOLD) return;
          dragActive = true;
          isDraggingRef.current = true;
          document.body.style.cursor = "grabbing";
        }

        if (type === "row") {
          setDragGhostRect({
            top: ev.clientY - sourceRect.height / 2,
            left: sourceRect.left,
            width: sourceRect.width,
            height: sourceRect.height,
          });
        } else {
          setDragGhostRect({
            top: sourceRect.top,
            left: ev.clientX - sourceRect.width / 2,
            width: sourceRect.width,
            height: sourceRect.height,
          });
        }

        if (type === "row") {
          const sourceRow = dom.querySelectorAll("tr")[tableRowIndex] as
            | HTMLTableRowElement
            | undefined;
          const sourceTableEl = sourceRow?.closest("table");
          if (!sourceTableEl) return;
          const tableRows = Array.from(sourceTableEl.querySelectorAll("tr"));
          const allDocRows = Array.from(dom.querySelectorAll("tr"));
          const tableStartGlobal = allDocRows.indexOf(tableRows[0] as HTMLTableRowElement);
          const rects = tableRows.map((r) => r.getBoundingClientRect());
          const localIdx = rowIndexAtY(rects, ev.clientY);
          if (localIdx < 0) return;
          const idx = tableStartGlobal + localIdx;
          if (idx === dropTarget) return;
          dropTarget = idx;

          if (idx !== fromIndex) {
            setDragTargetRect({
              top: rects[localIdx].top,
              left: rects[localIdx].left,
              width: rects[localIdx].right - rects[localIdx].left,
              height: rects[localIdx].bottom - rects[localIdx].top,
            });
          } else {
            setDragTargetRect(null);
          }

          const insertAfter = idx > fromIndex;
          setDropIndicator({
            top: insertAfter ? rects[localIdx].bottom - 1 : rects[localIdx].top - 1,
            left: rects[localIdx].left,
            width: rects[localIdx].right - rects[localIdx].left,
            height: 3,
          });
        } else {
          const sourceRow = dom.querySelectorAll("tr")[tableRowIndex] as
            | HTMLTableRowElement
            | undefined;
          const tableEl = sourceRow?.closest("table");
          const firstRow = tableEl?.querySelector("tr") as HTMLTableRowElement | null;
          if (!firstRow) return;
          const cellRects = Array.from(firstRow.cells).map((c) => {
            const r = c.getBoundingClientRect();
            return { left: r.left, right: r.right, cellIndex: c.cellIndex };
          });
          const idx = colIndexAtX(cellRects, ev.clientX);
          if (idx < 0 || idx === dropTarget) return;
          dropTarget = idx;

          const matched = cellRects.find((r) => r.cellIndex === idx);
          if (matched && tableEl && idx !== fromIndex) {
            const tableRect = tableEl.getBoundingClientRect();
            setDragTargetRect({
              top: tableRect.top,
              left: matched.left,
              width: matched.right - matched.left,
              height: tableRect.height,
            });
          } else {
            setDragTargetRect(null);
          }

          if (matched && tableEl) {
            const tableRect = tableEl.getBoundingClientRect();
            const insertAfter = idx > fromIndex;
            setDropIndicator({
              top: tableRect.top,
              left: insertAfter ? matched.right - 1 : matched.left - 1,
              width: 3,
              height: tableRect.height,
            });
          }
        }
      };

      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        dragCleanupRef.current = null;
        isDraggingRef.current = false;
        setRowSelRect(null);
        setColSelRect(null);
        setDragTargetRect(null);
        setDragGhostRect(null);
        setDropIndicator(null);

        if (dragActive) {
          wasDragRef.current = true;
          if (dropTarget !== null && dropTarget !== fromIndex) {
            if (type === "row") moveRowToIndex(editor, fromIndex, dropTarget);
            else moveColumnToIndex(editor, fromIndex, dropTarget, tableRowIndex);
          }
        }
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      dragCleanupRef.current = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        isDraggingRef.current = false;
        setRowSelRect(null);
        setColSelRect(null);
        setDragTargetRect(null);
        setDragGhostRect(null);
        setDropIndicator(null);
      };
    },
    [
      editor,
      isDraggingRef,
      wasDragRef,
      dragCleanupRef,
      setRowSelRect,
      setColSelRect,
      setDragTargetRect,
      setDragGhostRect,
      setDropIndicator,
    ],
  );

  return { startDrag };
}