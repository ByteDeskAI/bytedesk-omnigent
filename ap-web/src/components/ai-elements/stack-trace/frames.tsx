"use client";

import { cn } from "@/lib/utils";
import type { ComponentProps } from "react";
import { memo, useCallback } from "react";

import { useStackTrace } from "./context";
import type { StackFrame } from "./types";

const AT_PREFIX_REGEX = /^at\s+/;

interface FilePathButtonProps {
  frame: StackFrame;
  onFilePathClick?: (filePath: string, lineNumber?: number, columnNumber?: number) => void;
}

const FilePathButton = memo(({ frame, onFilePathClick }: FilePathButtonProps) => {
  const handleClick = useCallback(() => {
    if (frame.filePath) {
      onFilePathClick?.(
        frame.filePath,
        frame.lineNumber ?? undefined,
        frame.columnNumber ?? undefined,
      );
    }
  }, [frame, onFilePathClick]);

  return (
    <button
      className={cn(
        "underline decoration-dotted hover:text-primary",
        onFilePathClick && "cursor-pointer",
      )}
      disabled={!onFilePathClick}
      onClick={handleClick}
      type="button"
    >
      {frame.filePath}
      {frame.lineNumber !== null && `:${frame.lineNumber}`}
      {frame.columnNumber !== null && `:${frame.columnNumber}`}
    </button>
  );
});

FilePathButton.displayName = "FilePathButton";

export type StackTraceFramesProps = ComponentProps<"div"> & {
  showInternalFrames?: boolean;
};

export const StackTraceFrames = memo(
  ({ className, showInternalFrames = true, ...props }: StackTraceFramesProps) => {
    const { trace, onFilePathClick } = useStackTrace();

    const framesToShow = showInternalFrames
      ? trace.frames
      : trace.frames.filter((f) => !f.isInternal);

    return (
      <div className={cn("space-y-1 p-3", className)} {...props}>
        {framesToShow.map((frame) => (
          <div
            className={cn(
              "text-xs",
              frame.isInternal ? "text-muted-foreground/50" : "text-foreground/90",
            )}
            key={frame.raw}
          >
            <span className="text-muted-foreground">at </span>
            {frame.functionName && (
              <span className={frame.isInternal ? "" : "text-foreground"}>
                {frame.functionName}{" "}
              </span>
            )}
            {frame.filePath && (
              <>
                <span className="text-muted-foreground">(</span>
                <FilePathButton frame={frame} onFilePathClick={onFilePathClick} />
                <span className="text-muted-foreground">)</span>
              </>
            )}
            {!(frame.filePath || frame.functionName) && (
              <span>{frame.raw.replace(AT_PREFIX_REGEX, "")}</span>
            )}
          </div>
        ))}
        {framesToShow.length === 0 && (
          <div className="text-muted-foreground text-xs">No stack frames</div>
        )}
      </div>
    );
  },
);

StackTraceFrames.displayName = "StackTraceFrames";