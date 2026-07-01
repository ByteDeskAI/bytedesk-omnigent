import { useMemo } from "react";
import { ChevronRightIcon } from "lucide-react";
import { formatToolTitle } from "@/lib/toolTitle";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { RenderItem, ToolState } from "@/lib/renderItems";
import { useFileViewer } from "@/shell/FileViewerContext";
import { prettyPrintIfJson } from "./tool-card-utils";
import {
  CodePanel,
  EmptyOutputState,
  OutputSection,
  ToolPendingOutput,
  ToolTriggerRow,
  useElapsedDuration,
} from "./ToolCardParts";

const FILE_PATH_TOOLS = new Set(["sys_os_read", "sys_os_write", "sys_os_edit"]);

export interface ToolCardProps {
  /** Display name for the tool. For native tools, this is the friendly label. */
  name: string;
  /**
   * Set for native (provider-managed) tools — the underlying type
   * (e.g. "web_search_call"). Used to pick the category icon.
   */
  nativeToolType?: string;
  /** Brief one-line summary of arguments shown next to the name. */
  argsSummary?: string;
  /** Full args dict, rendered as JSON in the expanded panel. */
  arguments: Record<string, unknown>;
  /** Tool output, or null if not yet available / never produced. */
  output: string | null;
  state: ToolState;
  /** Seconds from the page's performance clock when the tool call rendered. */
  startedAt?: number | null;
  /** Completed runtime in seconds. Undefined when historical data lacks timing. */
  duration?: number;
}

export function ToolCard({
  name,
  nativeToolType,
  argsSummary,
  arguments: args,
  output,
  state,
  startedAt,
  duration,
}: ToolCardProps) {
  const title = useMemo(() => formatToolTitle(name, args, argsSummary), [name, args, argsSummary]);
  const inputJson = useMemo(() => JSON.stringify(args, null, 2), [args]);
  const formattedOutput = useMemo(
    () => (output === null ? null : prettyPrintIfJson(output)),
    [output],
  );
  const elapsedDuration = useElapsedDuration(state === "input-available" ? startedAt : null);
  const displayDuration = duration ?? elapsedDuration;

  // When this is a file-path tool and we're inside AppShell, make the path
  // in the trigger row a clickable link that opens the FileViewer.
  const openFile = useFileViewer();
  const rawPath =
    FILE_PATH_TOOLS.has(name) &&
    typeof args.path === "string" &&
    args.path.length > 0 &&
    !args.path.startsWith("/") // FileViewer rejects absolute paths
      ? args.path
      : null;
  const onBodyClick = openFile && rawPath ? () => openFile(rawPath) : undefined;

  return (
    <Collapsible defaultOpen={false} className="group not-prose w-full">
      <ToolTriggerRow
        title={title}
        name={name}
        nativeToolType={nativeToolType}
        state={state}
        duration={displayDuration}
        onBodyClick={onBodyClick}
      />
      <CollapsibleContent className="mt-1 ml-2 space-y-2 border-l pl-3 py-1 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
        <CodePanel
          title="Parameters"
          text={inputJson}
          copyText={inputJson}
          copyLabel="Copy parameters"
        />
        {formattedOutput !== null && <OutputSection output={formattedOutput} />}
        {formattedOutput === null && state === "input-available" && (
          <ToolPendingOutput duration={displayDuration} />
        )}
        {formattedOutput === null &&
          (state === "output-error" || state === "cancelled" || state === "no-output") && (
            <EmptyOutputState state={state} />
          )}
      </CollapsibleContent>
    </Collapsible>
  );
}

/**
 * Render a contiguous run of tool calls as one muted "See N steps" line.
 * Clicking expands to show each tool as its own (also-collapsible)
 * trigger. `BlockRenderer` decides which tools fold here (older tools
 * once a streaming-tail of the most recent ones has been peeled off,
 * or all completed tools once streaming finishes).
 */
export function ToolGroupSummary({ tools, count }: { tools: RenderItem[]; count?: number }) {
  // Label the FULL contiguous run, not just the folded tools — during
  // streaming the most-recent tools render as a visible tail outside this
  // group, so counting only `tools` would undercount ("See 2 steps" when
  // there are more visible). `count` defaults to the folded length for
  // fully-collapsed runs (reload / idle), where they're equal.
  const n = count ?? tools.length;
  const label = `See ${n} step${n === 1 ? "" : "s"}`;
  return (
    // Named `group/tool-summary` so this collapsible only rotates its
    // OWN chevron (line 296 in `ToolTriggerRow` uses an unnamed
    // `group-data-[state=open]:rotate-90` that would otherwise match
    // any ancestor `.group[data-state=open]` and incorrectly rotate
    // chevrons of inner tool cards when this outer group is open).
    // `peer` lets `BlockRenderer`'s trailing tail react to this
    // collapsible's open/closed state for the border-join effect.
    <Collapsible defaultOpen={false} className="group/tool-summary peer not-prose w-full">
      <CollapsibleTrigger className="flex cursor-pointer items-center gap-1.5 py-0.5 text-left text-muted-foreground text-xs transition-colors hover:text-foreground">
        <ChevronRightIcon className="size-3.5 shrink-0 transition-transform group-data-[state=open]/tool-summary:rotate-90" />
        <span>{label}</span>
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-1 ml-2 space-y-1 border-l pl-3 pt-1 pb-0 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
        {tools.map((item) => {
          if (item.kind === "tool") {
            return (
              <ToolCard
                key={`tool:${item.execution.callId}`}
                name={item.execution.name}
                argsSummary={item.execution.argsSummary}
                arguments={item.execution.arguments}
                output={item.output}
                state={item.state}
                startedAt={item.startedAt}
                duration={item.duration}
              />
            );
          }
          if (item.kind === "native_tool") {
            return (
              <ToolCard
                key={`native:${item.itemId ?? item.label}`}
                name={item.label}
                nativeToolType={item.toolType}
                arguments={item.data}
                output={null}
                state="output-available"
              />
            );
          }
          return null;
        })}
      </CollapsibleContent>
    </Collapsible>
  );
}

/**
 * Single muted-text trigger line for a tool call. Status/category icon
 * at left, title (verb bold + dynamic body) in the middle truncated to
 * one line, optional duration on the right, chevron at the far right.
 */
