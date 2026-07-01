import type { Editor } from "@tiptap/react";
import { TextSelection } from "@tiptap/pm/state";
import { Fragment } from "@tiptap/pm/model";
import type { Node as PMNode } from "@tiptap/pm/model";

export function freshCellPos(editor: Editor, rowIndex: number, colIndex: number): number | null {
  const rows = editor.view.dom.querySelectorAll("tr");
  const row = rows[rowIndex] as HTMLTableRowElement | undefined;
  if (!row) return null;
  const cell = row.cells[colIndex];
  if (!cell) return null;
  try {
    return editor.view.posAtDOM(cell, 0);
  } catch {
    return null;
  }
}

function getTableContext(editor: Editor, rowIndex: number): { node: PMNode; pos: number } | null {
  const pos = freshCellPos(editor, rowIndex, 0);
  if (pos === null) return null;
  const $pos = editor.state.doc.resolve(pos);
  for (let depth = $pos.depth; depth > 0; depth--) {
    const node = $pos.node(depth);
    if (node.type.name === "table") {
      return { node, pos: $pos.before(depth) };
    }
  }
  return null;
}

export function moveRowToIndex(editor: Editor, fromIndex: number, toIndex: number): void {
  if (fromIndex === toIndex) return;
  const ctx = getTableContext(editor, fromIndex);
  if (!ctx) return;
  const { node, pos } = ctx;
  const tableStart = getFirstGlobalRowInTable(editor, fromIndex);
  if (tableStart === null) return;
  const localFrom = fromIndex - tableStart;
  const localTo = toIndex - tableStart;
  if (localTo < 0 || localTo >= node.childCount) return;
  const rows = Array.from({ length: node.childCount }, (_, i) => node.child(i));
  const [row] = rows.splice(localFrom, 1);
  rows.splice(localTo, 0, row);
  const newTable = node.type.create(node.attrs, Fragment.fromArray(rows));
  editor.view.dispatch(editor.state.tr.replaceWith(pos, pos + node.nodeSize, newTable));
}

export function moveColumnToIndex(
  editor: Editor,
  fromCol: number,
  toCol: number,
  tableRowIndex: number,
): void {
  if (fromCol === toCol) return;
  const ctx = getTableContext(editor, tableRowIndex);
  if (!ctx) return;
  const { node, pos } = ctx;
  const newRows = Array.from({ length: node.childCount }, (_, r) => {
    const row = node.child(r);
    if (fromCol < 0 || fromCol >= row.childCount) return row;
    if (toCol < 0 || toCol >= row.childCount) return row;
    const cells = Array.from({ length: row.childCount }, (_, c) => row.child(c));
    const [cell] = cells.splice(fromCol, 1);
    cells.splice(toCol, 0, cell);
    return row.type.create(row.attrs, Fragment.fromArray(cells));
  });
  const newTable = node.type.create(node.attrs, Fragment.fromArray(newRows));
  editor.view.dispatch(editor.state.tr.replaceWith(pos, pos + node.nodeSize, newTable));
}

export function rowIndexAtY(
  rowRects: ReadonlyArray<{ top: number; bottom: number }>,
  y: number,
): number {
  for (let i = 0; i < rowRects.length; i++) {
    if (y >= rowRects[i].top && y < rowRects[i].bottom) return i;
  }
  return -1;
}

export function colIndexAtX(
  cellRects: ReadonlyArray<{ left: number; right: number; cellIndex: number }>,
  x: number,
): number {
  for (const r of cellRects) {
    if (x >= r.left && x < r.right) return r.cellIndex;
  }
  return -1;
}

function getFirstGlobalRowInTable(editor: Editor, anyRowIndex: number): number | null {
  const pos = freshCellPos(editor, anyRowIndex, 0);
  if (pos === null) return null;
  const $pos = editor.state.doc.resolve(pos);
  let tablePos: number | null = null;
  for (let depth = $pos.depth; depth > 0; depth--) {
    if ($pos.node(depth).type.name === "table") {
      tablePos = $pos.before(depth);
      break;
    }
  }
  if (tablePos === null) return null;
  const allGlobalRows = Array.from(editor.view.dom.querySelectorAll("tr"));
  for (let i = 0; i < allGlobalRows.length; i++) {
    try {
      const p = editor.view.posAtDOM(allGlobalRows[i], 0);
      const $p = editor.state.doc.resolve(p);
      for (let d = $p.depth; d > 0; d--) {
        if ($p.node(d).type.name === "table" && $p.before(d) === tablePos) {
          return i;
        }
      }
    } catch {
      // skip
    }
  }
  return null;
}

export function setCursorToCell(editor: Editor, rowIndex: number, colIndex: number): void {
  const pos = freshCellPos(editor, rowIndex, colIndex);
  if (pos === null) return;
  try {
    editor.view.dispatch(
      editor.state.tr.setSelection(TextSelection.create(editor.state.doc, pos + 1)),
    );
  } catch {
    // ignore stale positions
  }
}