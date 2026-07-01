import type { ReactNode } from "react";
import type { RenderItem } from "@/lib/renderItems";
import type { SessionStatus } from "@/lib/types";
import { ToolGroupSummary } from "../tool-card";
import { findStreamingRunStart, isToolItem, partitionToolRun } from "./block-renderer-utils";
import { renderItem } from "./renderItem";

interface BlockRendererProps {
  items: RenderItem[];
  sessionStatus: SessionStatus;
  conversationId?: string | null;
}

export function BlockRenderer({ items, sessionStatus, conversationId = null }: BlockRendererProps) {
  const rendered: ReactNode[] = [];
  const isAgentActive = sessionStatus === "running" || sessionStatus === "waiting";
  const streamingRunStart = isAgentActive ? findStreamingRunStart(items) : -1;
  const lastIdx = items.length - 1;
  const reasoningStreamingIdx =
    isAgentActive && lastIdx >= 0 && items[lastIdx]!.kind === "reasoning" ? lastIdx : -1;

  for (let i = 0; i < items.length; i += 1) {
    const item = items[i]!;

    if (isToolItem(item)) {
      const runStart = i;
      while (i < items.length && isToolItem(items[i]!)) i += 1;
      const run = items.slice(runStart, i);
      i -= 1;

      const { grouped, standalone } = partitionToolRun(run, runStart === streamingRunStart);

      if (grouped.length > 0) {
        rendered.push(
          <div key={`tool-group-with-tail:${runStart}`}>
            <ToolGroupSummary tools={grouped} count={run.length} />
            {standalone.length > 0 && (
              <div className="mt-1 ml-2 space-y-1 border-l pl-3 py-1 peer-data-[state=open]:mt-0">
                {standalone.map((tool, idx) =>
                  renderItem(tool, runStart + idx, false, conversationId),
                )}
              </div>
            )}
          </div>,
        );
      } else {
        for (const tool of standalone) {
          rendered.push(renderItem(tool, runStart, false, conversationId));
        }
      }
      continue;
    }

    rendered.push(renderItem(item, i, i === reasoningStreamingIdx, conversationId));
  }

  return <>{rendered}</>;
}

export { FilePathAwareMessageResponse } from "./FilePathAwareMessageResponse";
