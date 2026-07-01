export interface Rect {
  top: number;
  left: number;
  width: number;
  height: number;
}

export interface HandlePos {
  top: number;
  left: number;
  rowIndex: number;
  colIndex: number;
  cellWidth: number;
  rowHeight: number;
  rowWidth: number;
  tableHeight: number;
}

export const DRAG_THRESHOLD = 5;