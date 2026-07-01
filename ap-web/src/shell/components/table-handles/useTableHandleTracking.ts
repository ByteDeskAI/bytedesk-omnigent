import { useCallback, useEffect, useRef } from "react";
import type { Editor } from "@tiptap/react";
import type { HandlePos } from "./tableHandleTypes";

export function useTableHandleTracking(
  editor: Editor,
  scrollContainerRef: React.RefObject<HTMLDivElement | null>,
  {
    setRowHandle,
    setColHandle,
    setRowMenu,
    setColMenu,
    setRowSelRect,
    setColSelRect,
    setDragTargetRect,
    setDragGhostRect,
    setDropIndicator,
    setTableContextMenu,
    isDraggingRef,
    mouseInUI,
  }: {
    setRowHandle: React.Dispatch<React.SetStateAction<HandlePos | null>>;
    setColHandle: React.Dispatch<React.SetStateAction<HandlePos | null>>;
    setRowMenu: React.Dispatch<
      React.SetStateAction<{ anchorTop: number; anchorLeft: number; handle: HandlePos } | null>
    >;
    setColMenu: React.Dispatch<
      React.SetStateAction<{ anchorTop: number; anchorLeft: number; handle: HandlePos } | null>
    >;
    setRowSelRect: React.Dispatch<React.SetStateAction<import("./tableHandleTypes").Rect | null>>;
    setColSelRect: React.Dispatch<React.SetStateAction<import("./tableHandleTypes").Rect | null>>;
    setDragTargetRect: React.Dispatch<
      React.SetStateAction<import("./tableHandleTypes").Rect | null>
    >;
    setDragGhostRect: React.Dispatch<
      React.SetStateAction<import("./tableHandleTypes").Rect | null>
    >;
    setDropIndicator: React.Dispatch<
      React.SetStateAction<import("./tableHandleTypes").Rect | null>
    >;
    setTableContextMenu: React.Dispatch<
      React.SetStateAction<{ x: number; y: number; rowIndex: number; colIndex: number } | null>
    >;
    isDraggingRef: React.MutableRefObject<boolean>;
    mouseInUI: React.MutableRefObject<boolean>;
  },
) {
  const hideTimer = useRef<number>(0);

  const scheduleHide = useCallback(() => {
    window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => {
      if (!mouseInUI.current && !isDraggingRef.current) {
        setRowHandle(null);
        setColHandle(null);
        setRowSelRect(null);
        setColSelRect(null);
      }
    }, 150);
  }, [isDraggingRef, mouseInUI, setColHandle, setColSelRect, setRowHandle, setRowSelRect]);

  const cancelHide = useCallback(() => {
    window.clearTimeout(hideTimer.current);
  }, []);

  useEffect(() => {
    if (!editor.view?.dom) return;
    const dom = editor.view.dom;

    const onMouseMove = (e: MouseEvent) => {
      if (!(e.target instanceof Element)) return;
      const target = e.target;
      const cell = target.closest("td, th");
      const row = target.closest("tr");
      const table = target.closest("table");
      if (!cell || !row || !table) {
        scheduleHide();
        return;
      }
      cancelHide();

      const allDocRows = Array.from(editor.view.dom.querySelectorAll("tr"));
      const rowIndex = allDocRows.indexOf(row as HTMLTableRowElement);
      const colIndex = (cell as HTMLTableCellElement).cellIndex;
      const rowRect = row.getBoundingClientRect();
      const cellRect = cell.getBoundingClientRect();
      const tableRect = table.getBoundingClientRect();

      setRowHandle((prev) => {
        if (
          prev?.rowIndex === rowIndex &&
          prev.colIndex === colIndex &&
          prev.top === rowRect.top &&
          prev.left === rowRect.left
        )
          return prev;
        return {
          top: rowRect.top,
          left: rowRect.left,
          rowIndex,
          colIndex,
          cellWidth: cellRect.width,
          rowHeight: rowRect.height,
          rowWidth: rowRect.width,
          tableHeight: tableRect.height,
        };
      });
      setColHandle((prev) => {
        if (
          prev?.rowIndex === rowIndex &&
          prev.colIndex === colIndex &&
          prev.top === tableRect.top &&
          prev.left === cellRect.left
        )
          return prev;
        return {
          top: tableRect.top,
          left: cellRect.left,
          rowIndex,
          colIndex,
          cellWidth: cellRect.width,
          rowHeight: rowRect.height,
          rowWidth: rowRect.width,
          tableHeight: tableRect.height,
        };
      });
    };

    const onContextMenu = (e: MouseEvent) => {
      if (!(e.target instanceof Element)) return;
      const target = e.target;
      if (!target.closest("td, th")) return;
      e.preventDefault();
      const cell = target.closest("td, th") as HTMLTableCellElement;
      const row = target.closest("tr") as HTMLTableRowElement | null;
      const table = target.closest("table");
      if (!row || !table) return;
      const allDocRows = Array.from(dom.querySelectorAll("tr"));
      const rowIndex = allDocRows.indexOf(row);
      const colIndex = cell.cellIndex;
      setTableContextMenu({ x: e.clientX, y: e.clientY, rowIndex, colIndex });
    };

    const onMouseLeave = () => scheduleHide();
    const onScroll = () => {
      setRowHandle(null);
      setColHandle(null);
      setRowMenu(null);
      setColMenu(null);
      setRowSelRect(null);
      setColSelRect(null);
      setDragTargetRect(null);
      setDragGhostRect(null);
      setDropIndicator(null);
      setTableContextMenu(null);
    };

    dom.addEventListener("mousemove", onMouseMove);
    dom.addEventListener("mouseleave", onMouseLeave);
    dom.addEventListener("contextmenu", onContextMenu);
    const scrollEl = scrollContainerRef.current;
    scrollEl?.addEventListener("scroll", onScroll);

    return () => {
      dom.removeEventListener("mousemove", onMouseMove);
      dom.removeEventListener("mouseleave", onMouseLeave);
      dom.removeEventListener("contextmenu", onContextMenu);
      scrollEl?.removeEventListener("scroll", onScroll);
      window.clearTimeout(hideTimer.current);
    };
  }, [
    editor,
    scheduleHide,
    cancelHide,
    scrollContainerRef,
    setRowHandle,
    setColHandle,
    setRowMenu,
    setColMenu,
    setRowSelRect,
    setColSelRect,
    setDragTargetRect,
    setDragGhostRect,
    setDropIndicator,
    setTableContextMenu,
  ]);

  useEffect(() => {
    if (!editor.view?.dom) return;
    const dom = editor.view.dom;
    let rafId = 0;
    const fixResizeHandle = () => {
      cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => {
        const handle = dom.querySelector<HTMLElement>(".column-resize-handle");
        if (!handle) return;
        const cell = handle.closest<HTMLElement>("td, th");
        const table = handle.closest<HTMLElement>("table");
        if (!cell || !table) return;
        const topOffset = cell.getBoundingClientRect().top - table.getBoundingClientRect().top;
        handle.style.top = `-${topOffset}px`;
        handle.style.height = `${table.offsetHeight}px`;
      });
    };
    const observer = new MutationObserver(fixResizeHandle);
    observer.observe(dom, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      cancelAnimationFrame(rafId);
    };
  }, [editor]);

  return { scheduleHide, cancelHide };
}