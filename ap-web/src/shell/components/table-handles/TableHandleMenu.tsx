import { useEffect } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";

export type MenuItemDef =
  | { label: string; icon: React.ReactNode; onClick: () => void; destructive?: boolean }
  | { separator: true };

export function TableHandleMenu({
  items,
  anchorTop,
  anchorLeft,
  onClose,
  onMouseEnter,
  onMouseLeave,
}: {
  items: MenuItemDef[];
  anchorTop: number;
  anchorLeft: number;
  onClose: () => void;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
}) {
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (!(e.target instanceof Element)) return;
      if (!e.target.closest("[data-table-handle-menu]")) {
        onClose();
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  return createPortal(
    <div
      data-table-handle-menu
      className="fixed z-[9999] min-w-[180px] overflow-hidden rounded-md border border-border bg-popover py-1 shadow-md"
      style={{ top: anchorTop, left: anchorLeft }}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      {items.map((item, i) =>
        "separator" in item ? (
          <div key={i} className="mx-2 my-1 h-px bg-border" />
        ) : (
          <button
            key={i}
            type="button"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              item.onClick();
              onClose();
            }}
            className={cn(
              "flex w-full items-center gap-2.5 px-3 py-1.5 text-xs transition-colors",
              item.destructive
                ? "text-destructive hover:bg-destructive/10"
                : "text-foreground hover:bg-muted",
            )}
          >
            {item.icon}
            {item.label}
          </button>
        ),
      )}
    </div>,
    document.body,
  );
}