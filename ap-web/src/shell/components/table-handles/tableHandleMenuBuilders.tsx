import { TextSelection } from "@tiptap/pm/state";
import { Trash2 } from "lucide-react";
import type { Editor } from "@tiptap/react";
import { freshCellPos, setCursorToCell } from "./tableBubbleMenuUtils";
import type { MenuItemDef } from "./TableHandleMenu";
import type { HandlePos } from "./tableHandleTypes";

export function buildRowMenuItems(editor: Editor, h: HandlePos): MenuItemDef[] {
  return [
    {
      label: "Insert row above",
      icon: <span className="text-[10px] font-bold">↑</span>,
      onClick: () => {
        setCursorToCell(editor, h.rowIndex, 0);
        editor.chain().focus().addRowBefore().run();
      },
    },
    {
      label: "Insert row below",
      icon: <span className="text-[10px] font-bold">↓</span>,
      onClick: () => {
        setCursorToCell(editor, h.rowIndex, 0);
        editor.chain().focus().addRowAfter().run();
      },
    },
    { separator: true },
    {
      label: "Delete row",
      icon: <Trash2 className="size-3.5" />,
      destructive: true,
      onClick: () => {
        setCursorToCell(editor, h.rowIndex, 0);
        editor.chain().focus().deleteRow().run();
      },
    },
  ];
}

export function buildColMenuItems(editor: Editor, h: HandlePos): MenuItemDef[] {
  return [
    {
      label: "Insert column before",
      icon: <span className="text-[10px] font-bold">←</span>,
      onClick: () => {
        setCursorToCell(editor, h.rowIndex, h.colIndex);
        editor.chain().focus().addColumnBefore().run();
      },
    },
    {
      label: "Insert column after",
      icon: <span className="text-[10px] font-bold">→</span>,
      onClick: () => {
        setCursorToCell(editor, h.rowIndex, h.colIndex);
        editor.chain().focus().addColumnAfter().run();
      },
    },
    { separator: true },
    {
      label: "Delete column",
      icon: <Trash2 className="size-3.5" />,
      destructive: true,
      onClick: () => {
        const pos = freshCellPos(editor, h.rowIndex, h.colIndex);
        if (pos !== null) {
          editor.view.dispatch(
            editor.state.tr.setSelection(TextSelection.create(editor.state.doc, pos + 1)),
          );
          editor.chain().focus().deleteColumn().run();
        }
      },
    },
  ];
}