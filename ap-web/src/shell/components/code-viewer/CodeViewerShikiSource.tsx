import type { RefObject } from "react";
import type { ThemedToken } from "shiki";
import { type Comment } from "@/hooks/useComments";
import { cn } from "@/lib/utils";
import { type ActiveSelection, indexToLine, lineOverlapsSelection } from "../../codeViewerHelpers";
import { renderLineTokens } from "../../codeViewerRendering";

export function CodeViewerShikiSource({
  codeContainerRef,
  rawLines,
  tokenLines,
  comments,
  activeSelection,
  onSetActiveSelection,
  searchQuery,
  matches,
  safeMatchIdx,
  matchLineRefs,
}: {
  codeContainerRef: RefObject<HTMLDivElement | null>;
  rawLines: string[];
  tokenLines: ThemedToken[][] | null;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: {
    start_index: number;
    end_index: number;
    anchor_content: string;
  }) => void;
  searchQuery: string;
  matches: number[];
  safeMatchIdx: number;
  matchLineRefs: RefObject<Map<number, HTMLDivElement>>;
}) {
  const commentByLine = new Map<number, Comment>();
  for (const c of comments) {
    const ln = indexToLine(c.start_index, rawLines);
    if (!commentByLine.has(ln)) commentByLine.set(ln, c);
  }

  const lineStarts: number[] = [];
  {
    let off = 0;
    for (const l of rawLines) {
      lineStarts.push(off);
      off += l.length + 1;
    }
  }

  return (
    <div ref={codeContainerRef} className="font-mono text-xs bg-white dark:bg-[#0d1117]">
      {rawLines.map((rawLine, idx) => {
        const lineNum = idx + 1;
        const isMatchLine =
          searchQuery.trim() !== "" &&
          rawLines[idx].toLowerCase().includes(searchQuery.toLowerCase());
        const isCurrentMatch = isMatchLine && matches[safeMatchIdx] === idx;
        const commentOnLine = commentByLine.get(lineNum);
        const isActiveRange =
          activeSelection != null &&
          lineOverlapsSelection(idx, rawLines, activeSelection.start_index, activeSelection.end_index);
        const tokens = tokenLines?.[idx] ?? null;
        const lineAbsStart = lineStarts[idx] ?? 0;
        const selStartCol = isActiveRange
          ? Math.max(0, activeSelection!.start_index - lineAbsStart)
          : 0;
        const selEndCol = isActiveRange
          ? Math.min(rawLine.length, activeSelection!.end_index - lineAbsStart)
          : 0;
        const commentOverlays = comments
          .filter((c) => lineOverlapsSelection(idx, rawLines, c.start_index, c.end_index))
          .map((c) => ({
            id: c.id,
            startCol: Math.max(0, c.start_index - lineAbsStart),
            endCol: Math.min(rawLine.length, c.end_index - lineAbsStart),
            isSelected:
              activeSelection?.start_index === c.start_index &&
              activeSelection?.end_index === c.end_index,
          }))
          .filter((o) => o.endCol > o.startCol);
        const hasAnyHighlight = commentOverlays.length > 0 || isActiveRange;

        return (
          <div
            key={lineNum}
            ref={(el) => {
              if (el) matchLineRefs.current.set(idx, el);
              else matchLineRefs.current.delete(idx);
            }}
            className={cn(isCurrentMatch && "bg-yellow-200/40 dark:bg-yellow-700/30")}
          >
            <div className="flex items-stretch">
              <div
                data-gutter-comment={commentOnLine ? true : undefined}
                className={cn(
                  "relative w-12 shrink-0 select-none border-r border-border text-xs",
                  "flex items-center justify-end px-2 py-0.5 leading-5",
                  commentOnLine
                    ? "cursor-pointer text-yellow-500 dark:text-yellow-400 hover:bg-muted/60"
                    : "text-muted-foreground/50",
                  hasAnyHighlight && "bg-yellow-500/10 dark:bg-yellow-400/15",
                )}
                onClick={() => {
                  if (commentOnLine) {
                    onSetActiveSelection({
                      start_index: commentOnLine.start_index,
                      end_index: commentOnLine.end_index,
                      anchor_content: commentOnLine.anchor_content ?? "",
                    });
                  }
                }}
              >
                <span>{lineNum}</span>
              </div>
              <div
                data-line={lineNum}
                className="relative flex-1 overflow-hidden whitespace-pre-wrap break-all pl-3 py-0.5 leading-5"
              >
                {commentOverlays.map((o) => (
                  <span
                    key={o.id}
                    aria-hidden
                    className={cn(
                      "absolute inset-y-0 pointer-events-none",
                      o.isSelected
                        ? "bg-yellow-400/25 dark:bg-yellow-400/25"
                        : "bg-yellow-200/40 dark:bg-yellow-400/20",
                    )}
                    style={{
                      left: `calc(0.75rem + ${o.startCol}ch)`,
                      width: `${o.endCol - o.startCol}ch`,
                    }}
                  />
                ))}
                {isActiveRange &&
                  selEndCol > selStartCol &&
                  !comments.some(
                    (c) =>
                      c.start_index === activeSelection!.start_index &&
                      c.end_index === activeSelection!.end_index,
                  ) && (
                    <span
                      aria-hidden
                      className="absolute inset-y-0 bg-yellow-400/25 dark:bg-yellow-400/25 pointer-events-none"
                      style={{
                        left: `calc(0.75rem + ${selStartCol}ch)`,
                        width: `${selEndCol - selStartCol}ch`,
                      }}
                    />
                  )}
                {tokens !== null
                  ? renderLineTokens(tokens, isMatchLine ? searchQuery : "", isCurrentMatch)
                  : rawLine}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}