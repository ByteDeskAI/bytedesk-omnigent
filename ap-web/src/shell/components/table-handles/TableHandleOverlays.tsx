import { createPortal } from "react-dom";
import type { Rect } from "./tableHandleTypes";

export function TableHandleOverlays({
  rowSelRect,
  colSelRect,
  dragTargetRect,
  dragGhostRect,
  dropIndicator,
}: {
  rowSelRect: Rect | null;
  colSelRect: Rect | null;
  dragTargetRect: Rect | null;
  dragGhostRect: Rect | null;
  dropIndicator: Rect | null;
}) {
  return (
    <>
      {rowSelRect &&
        createPortal(
          <div
            className="pointer-events-none fixed z-40 border-2 border-primary bg-primary/5"
            style={{
              top: rowSelRect.top,
              left: rowSelRect.left,
              width: rowSelRect.width,
              height: rowSelRect.height,
            }}
          />,
          document.body,
        )}
      {colSelRect &&
        createPortal(
          <div
            className="pointer-events-none fixed z-40 border-2 border-primary bg-primary/5"
            style={{
              top: colSelRect.top,
              left: colSelRect.left,
              width: colSelRect.width,
              height: colSelRect.height,
            }}
          />,
          document.body,
        )}
      {dragTargetRect &&
        createPortal(
          <div
            className="pointer-events-none fixed z-40 border-2 border-primary/60 bg-primary/10"
            style={{
              top: dragTargetRect.top,
              left: dragTargetRect.left,
              width: dragTargetRect.width,
              height: dragTargetRect.height,
            }}
          />,
          document.body,
        )}
      {dragGhostRect &&
        createPortal(
          <div
            className="pointer-events-none fixed z-[9997] border-2 border-primary bg-primary/15 opacity-90"
            style={{
              top: dragGhostRect.top,
              left: dragGhostRect.left,
              width: dragGhostRect.width,
              height: dragGhostRect.height,
            }}
          />,
          document.body,
        )}
      {dropIndicator &&
        createPortal(
          <div
            className="pointer-events-none fixed z-[9999] rounded-sm bg-primary"
            style={{
              top: dropIndicator.top,
              left: dropIndicator.left,
              width: dropIndicator.width,
              height: dropIndicator.height,
            }}
          />,
          document.body,
        )}
    </>
  );
}