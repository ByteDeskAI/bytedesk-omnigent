import { useEffect, useRef, useState } from "react";
import type { Editor } from "@tiptap/react";
import type {} from "@tiptap/extension-table";
import { Trash2 } from "lucide-react";
import { TableHandleMenu } from "./TableHandleMenu";
import { buildColMenuItems, buildRowMenuItems } from "./tableHandleMenuBuilders";
import { TableColumnHandlePortal } from "./TableColumnHandlePortal";
import { TableHandleOverlays } from "./TableHandleOverlays";
import { TableRowHandlePortal } from "./TableRowHandlePortal";
import type { HandlePos, Rect } from "./tableHandleTypes";
import { useTableHandleDrag } from "./useTableHandleDrag";
import { useTableHandleTracking } from "./useTableHandleTracking";
import { setCursorToCell } from "./tableBubbleMenuUtils";

export function TableHandles({
  editor,
  scrollContainerRef,
}: {
  editor: Editor;
  scrollContainerRef: React.RefObject<HTMLDivElement | null>;
}) {
  const [rowHandle, setRowHandle] = useState<HandlePos | null>(null);
  const [colHandle, setColHandle] = useState<HandlePos | null>(null);
  const [rowMenu, setRowMenu] = useState<{
    anchorTop: number;
    anchorLeft: number;
    handle: HandlePos;
  } | null>(null);
  const [colMenu, setColMenu] = useState<{
    anchorTop: number;
    anchorLeft: number;
    handle: HandlePos;
  } | null>(null);
  const [rowSelRect, setRowSelRect] = useState<Rect | null>(null);
  const [colSelRect, setColSelRect] = useState<Rect | null>(null);
  const [tableContextMenu, setTableContextMenu] = useState<{
    x: number;
    y: number;
    rowIndex: number;
    colIndex: number;
  } | null>(null);
  const [dragTargetRect, setDragTargetRect] = useState<Rect | null>(null);
  const [dragGhostRect, setDragGhostRect] = useState<Rect | null>(null);
  const [dropIndicator, setDropIndicator] = useState<Rect | null>(null);

  const mouseInUI = useRef(false);
  const isDraggingRef = useRef(false);
  const wasDragRef = useRef(false);
  const dragCleanupRef = useRef<(() => void) | null>(null);

  useEffect(
    () => () => {
      dragCleanupRef.current?.();
    },
    [],
  );

  const { scheduleHide, cancelHide } = useTableHandleTracking(editor, scrollContainerRef, {
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
  });

  const { startDrag } = useTableHandleDrag(editor, {
    isDraggingRef,
    wasDragRef,
    dragCleanupRef,
    setRowSelRect,
    setColSelRect,
    setDragTargetRect,
    setDragGhostRect,
    setDropIndicator,
  });

  const handleMouseEnter = () => {
    mouseInUI.current = true;
    cancelHide();
  };
  const handleMouseLeave = () => {
    mouseInUI.current = false;
    scheduleHide();
  };

  return (
    <>
      <TableHandleOverlays
        rowSelRect={rowSelRect}
        colSelRect={colSelRect}
        dragTargetRect={dragTargetRect}
        dragGhostRect={dragGhostRect}
        dropIndicator={dropIndicator}
      />

      {rowHandle && (
        <TableRowHandlePortal
          rowHandle={rowHandle}
          rowMenuOpen={rowMenu !== null}
          isDraggingRef={isDraggingRef}
          wasDragRef={wasDragRef}
          cancelHide={cancelHide}
          scheduleHide={scheduleHide}
          mouseInUI={mouseInUI}
          setColSelRect={setColSelRect}
          setRowSelRect={setRowSelRect}
          startDrag={startDrag}
          setRowMenu={setRowMenu}
        />
      )}

      {rowMenu && (
        <TableHandleMenu
          items={buildRowMenuItems(editor, rowMenu.handle)}
          anchorTop={rowMenu.anchorTop}
          anchorLeft={rowMenu.anchorLeft}
          onClose={() => setRowMenu(null)}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        />
      )}

      {colHandle && (
        <TableColumnHandlePortal
          colHandle={colHandle}
          colMenuOpen={colMenu !== null}
          isDraggingRef={isDraggingRef}
          wasDragRef={wasDragRef}
          cancelHide={cancelHide}
          scheduleHide={scheduleHide}
          mouseInUI={mouseInUI}
          setRowSelRect={setRowSelRect}
          setColSelRect={setColSelRect}
          startDrag={startDrag}
          setColMenu={setColMenu}
        />
      )}

      {colMenu && (
        <TableHandleMenu
          items={buildColMenuItems(editor, colMenu.handle)}
          anchorTop={colMenu.anchorTop}
          anchorLeft={colMenu.anchorLeft}
          onClose={() => setColMenu(null)}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        />
      )}

      {tableContextMenu && (
        <TableHandleMenu
          items={[
            {
              label: "Delete table",
              icon: <Trash2 className="size-3.5" />,
              destructive: true,
              onClick: () => {
                setCursorToCell(
                  editor,
                  tableContextMenu.rowIndex,
                  tableContextMenu.colIndex,
                );
                editor.chain().focus().deleteTable().run();
              },
            },
          ]}
          anchorTop={tableContextMenu.y}
          anchorLeft={tableContextMenu.x}
          onClose={() => setTableContextMenu(null)}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        />
      )}
    </>
  );
}