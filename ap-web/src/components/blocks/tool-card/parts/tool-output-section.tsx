import { Loader2Icon, Maximize2Icon, Minimize2Icon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { formatToolDuration, getOutputPreview, type OutputPreview } from "../tool-card-utils";
import { CodePanel } from "./tool-code-panel";

function formatCount(count: number, unit: string): string {
  return `${count.toLocaleString()} ${unit}${count === 1 ? "" : "s"}`;
}

function formatOutputStats(preview: OutputPreview): string {
  if (!preview.isTruncated) {
    return `${formatCount(preview.lineCount, "line")} / ${formatCount(preview.charCount, "char")}`;
  }

  const hidden: string[] = [];
  if (preview.hiddenLineCount > 0) {
    hidden.push(`${formatCount(preview.hiddenLineCount, "line")} hidden`);
  }
  if (preview.hiddenCharCount > 0) {
    hidden.push(`${formatCount(preview.hiddenCharCount, "char")} hidden`);
  }

  return `${formatCount(preview.shownLineCount, "line")} / ${formatCount(
    preview.shownCharCount,
    "char",
  )} shown; ${hidden.join(", ")}`;
}

export function OutputSection({ output }: { output: string }) {
  const [isExpanded, setIsExpanded] = useState(false);
  useEffect(() => setIsExpanded(false), [output]);

  const collapsedPreview = useMemo(() => getOutputPreview(output), [output]);
  const preview = useMemo(() => getOutputPreview(output, isExpanded), [output, isExpanded]);
  const canExpand = collapsedPreview.isTruncated;

  return (
    <div className="space-y-2">
      <div
        className={cn(
          "relative rounded-md",
          canExpand && !isExpanded && "max-h-80 overflow-hidden",
          (!canExpand || isExpanded) && "max-h-[36rem] overflow-auto",
        )}
      >
        <CodePanel title="Output" text={preview.text} copyText={output} copyLabel="Copy output" />
        {canExpand && !isExpanded && (
          <div className="pointer-events-none absolute inset-x-px bottom-px h-16 rounded-b-md bg-gradient-to-t from-background to-transparent" />
        )}
      </div>
      {canExpand && (
        <div className="flex flex-col gap-2 rounded-md border bg-muted/30 px-3 py-2 text-muted-foreground text-xs sm:flex-row sm:items-center sm:justify-between">
          <span className="min-w-0">
            {isExpanded ? "Showing full output" : "Previewing output"} (
            {formatOutputStats(isExpanded ? preview : collapsedPreview)})
          </span>
          <Button
            className="w-fit"
            onClick={() => setIsExpanded((value) => !value)}
            size="xs"
            type="button"
            variant="outline"
          >
            {isExpanded ? (
              <Minimize2Icon className="size-3" />
            ) : (
              <Maximize2Icon className="size-3" />
            )}
            {isExpanded ? "Collapse" : "Expand"}
          </Button>
        </div>
      )}
    </div>
  );
}

export function ToolPendingOutput({ duration }: { duration: number | undefined }) {
  return (
    <div className="rounded-md border border-dashed bg-muted/30 p-3">
      <div className="flex items-center gap-2 text-muted-foreground text-sm">
        <Loader2Icon className="size-4 animate-spin text-info" />
        <span>
          Waiting for output
          {duration !== undefined ? ` for ${formatToolDuration(duration)}` : ""}
        </span>
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full w-1/3 animate-pulse rounded-full bg-info/70" />
      </div>
    </div>
  );
}