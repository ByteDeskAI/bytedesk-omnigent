import { useState } from "react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useEditorState } from "@tiptap/react";
import { AlignCenter, AlignLeft, AlignRight, Table2 } from "lucide-react";
import type { Editor } from "@tiptap/react";
import "@tiptap/markdown";
import type {} from "@tiptap/extension-table";
import { TableMap, cellAround, colCount, findTable, isInTable } from "@tiptap/pm/tables";
import { cn } from "@/lib/utils";
import { Divider, ToolbarBtn } from "./MarkdownToolbarPrimitives";

type ColumnAlign = "left" | "center" | "right";

function setColumnAlign(editor: Editor, align: ColumnAlign | null): boolean {
  const { state } = editor.view;
  if (!isInTable(state)) return false;
  const $cell = cellAround(state.selection.$head);
  if (!$cell) return false;
  const col = colCount($cell);
  const tableResult = findTable(state.selection.$from);
  if (!tableResult) return false;
  const map = TableMap.get(tableResult.node);
  const cellPositions = map.cellsInRect({
    left: col,
    right: col + 1,
    top: 0,
    bottom: map.height,
  });
  if (cellPositions.length === 0) return false;
  const tr = state.tr;
  let changed = false;
  cellPositions.forEach((nodePos) => {
    const node = tableResult.node.nodeAt(nodePos);
    const absPos = nodePos + tableResult.start;
    if (node && node.attrs.align !== align) {
      tr.setNodeMarkup(absPos, null, { ...node.attrs, align });
      changed = true;
    }
  });
  if (changed) {
    editor.view.dispatch(tr);
    editor.view.focus();
  }
  return true;
}

export function TableBtn({ editor }: { editor: Editor | null }) {
  const [open, setOpen] = useState(false);
  const [hovered, setHovered] = useState({ rows: 0, cols: 0 });
  const MAX = 6;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        title="Insert table"
        aria-label="Insert table"
        disabled={!editor}
        onMouseDown={(e) => e.preventDefault()}
        className="min-w-[1.75rem] rounded px-1.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
      >
        <Table2 className="size-3.5" />
      </PopoverTrigger>
      <PopoverContent
        className="w-auto p-2"
        align="start"
        onMouseLeave={() => setHovered({ rows: 0, cols: 0 })}
      >
        <p className="mb-1.5 text-xs text-muted-foreground">
          {hovered.rows > 0 ? `${hovered.rows} × ${hovered.cols} table` : "Insert table"}
        </p>
        <div className="flex flex-col gap-0.5">
          {Array.from({ length: MAX }, (_, r) => (
            <div key={r} className="flex gap-0.5">
              {Array.from({ length: MAX }, (_, c) => (
                <button
                  key={c}
                  type="button"
                  aria-label={`Insert ${r + 1}×${c + 1} table`}
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseEnter={() => setHovered({ rows: r + 1, cols: c + 1 })}
                  onClick={() => {
                    editor
                      ?.chain()
                      .focus()
                      .insertTable({
                        rows: r + 1,
                        cols: c + 1,
                        withHeaderRow: true,
                      })
                      .run();
                    setOpen(false);
                  }}
                  className={cn(
                    "h-5 w-5 cursor-pointer rounded-sm border transition-colors",
                    r < hovered.rows && c < hovered.cols
                      ? "border-primary bg-primary/20"
                      : "border-border bg-muted hover:border-primary/50 hover:bg-primary/10",
                  )}
                />
              ))}
            </div>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

export function TableAlignControls({ editor }: { editor: Editor }) {
  const state = useEditorState({
    editor,
    selector: (ctx) => ({
      inTable: (ctx.editor?.isActive("tableCell") || ctx.editor?.isActive("tableHeader")) ?? false,
      align:
        (ctx.editor?.getAttributes("tableCell").align as ColumnAlign | undefined) ??
        (ctx.editor?.getAttributes("tableHeader").align as ColumnAlign | undefined) ??
        null,
    }),
  });

  if (!state?.inTable) return null;

  const current = state.align;

  return (
    <>
      <Divider />
      <ToolbarBtn
        active={current === "left"}
        title="Align column left"
        onClick={() => setColumnAlign(editor, "left")}
      >
        <AlignLeft className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        active={current === "center"}
        title="Align column center"
        onClick={() => setColumnAlign(editor, "center")}
      >
        <AlignCenter className="size-3.5" />
      </ToolbarBtn>
      <ToolbarBtn
        active={current === "right"}
        title="Align column right"
        onClick={() => setColumnAlign(editor, "right")}
      >
        <AlignRight className="size-3.5" />
      </ToolbarBtn>
    </>
  );
}