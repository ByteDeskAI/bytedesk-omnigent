import {
  ChevronRightIcon,
  CircleSlashIcon,
  Loader2Icon,
  XCircleIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { CollapsibleTrigger } from "@/components/ui/collapsible";
import type { ToolState } from "@/lib/renderItems";
import { iconForTool } from "@/lib/toolIcon";
import { type ToolTitle } from "@/lib/toolTitle";
import { formatToolDuration } from "../tool-card-utils";

function StatusIcon({
  name,
  nativeToolType,
  state,
}: {
  name: string;
  nativeToolType: string | undefined;
  state: ToolState;
}): ReactNode {
  if (state === "input-available") {
    return <Loader2Icon className="size-3.5 shrink-0 animate-spin text-info" />;
  }
  if (state === "output-error") {
    return <XCircleIcon className="size-3.5 shrink-0 text-destructive" />;
  }
  if (state === "cancelled" || state === "no-output") {
    return <CircleSlashIcon className="size-3.5 shrink-0" />;
  }
  const Icon = iconForTool(name, nativeToolType);
  return <Icon className="size-3.5 shrink-0" />;
}

export function ToolTriggerRow({
  title,
  name,
  nativeToolType,
  state,
  duration,
  onBodyClick,
}: {
  title: ToolTitle;
  name: string;
  nativeToolType: string | undefined;
  state: ToolState;
  duration: number | undefined;
  onBodyClick?: () => void;
}) {
  const tooltip =
    title.verb && title.body ? `${title.verb} ${title.body}` : (title.verb ?? title.body);
  return (
    <CollapsibleTrigger
      title={tooltip}
      className="flex w-full cursor-pointer items-center gap-1.5 py-0.5 text-left text-muted-foreground text-xs transition-colors hover:text-foreground"
    >
      <StatusIcon name={name} nativeToolType={nativeToolType} state={state} />
      <span className="min-w-0 flex-1 truncate">
        {title.verb !== null && <span className="font-semibold text-foreground">{title.verb}</span>}
        {title.verb !== null && title.body.length > 0 && " "}
        {onBodyClick ? (
          <span
            role="link"
            tabIndex={0}
            className="underline decoration-dotted underline-offset-2 hover:text-foreground transition-colors cursor-pointer"
            onClick={(e) => {
              e.stopPropagation();
              onBodyClick();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                e.stopPropagation();
                onBodyClick();
              }
            }}
          >
            {title.body}
          </span>
        ) : (
          title.body
        )}
      </span>
      {duration !== undefined && (
        <span className="shrink-0 tabular-nums opacity-70">{formatToolDuration(duration)}</span>
      )}
      <ChevronRightIcon className="size-3.5 shrink-0 transition-transform group-data-[state=open]:rotate-90" />
    </CollapsibleTrigger>
  );
}