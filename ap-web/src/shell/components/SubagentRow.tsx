import { CornerDownRightIcon } from "lucide-react";
import { Link, useLocation } from "@/lib/routing";
import { MAX_TREE_DEPTH, useChildSessions, type ChildSessionInfo } from "@/hooks/useChildSessions";
import { cn } from "@/lib/utils";
import {
  ROW_BASE_PADDING_PX,
  ROW_DEPTH_STEP_PX,
  TREE_POLL_MS,
} from "./subagentsPanelConstants";
import {
  brandChildIcon,
  childPrimaryLabel,
  childStatus,
  iconForAgentType,
  railLinkSearch,
  SETTLED_STATE,
} from "./subagentsPanelUtils";
import { SubagentStatusIndicator } from "./SubagentStatusIndicator";

export function SubagentRow({
  child,
  depth,
  conversationId,
}: {
  child: ChildSessionInfo;
  depth: number;
  conversationId: string;
}) {
  const status = childStatus(child);
  const search = railLinkSearch(useLocation().search);
  const Icon = brandChildIcon(child) ?? iconForAgentType(child.tool);
  const primary = childPrimaryLabel(child);
  const isActive = conversationId === child.id;
  const dim = !isActive && SETTLED_STATE[status.activity];
  const { children: grandchildren } = useChildSessions(
    depth < MAX_TREE_DEPTH ? child.id : null,
    TREE_POLL_MS,
  );
  return (
    <>
      <li>
        <Link
          to={{ pathname: `/c/${child.id}`, search }}
          data-testid="subagent-row"
          data-child-session-id={child.id}
          data-depth={depth}
          style={{ paddingLeft: ROW_BASE_PADDING_PX + (depth - 1) * ROW_DEPTH_STEP_PX }}
          className={cn(
            "flex w-full flex-col gap-0.5 py-2 pr-2.5 text-left hover:bg-accent/60",
            isActive && "bg-accent",
            dim && "opacity-60 hover:opacity-100",
          )}
        >
          <div className="flex w-full items-center gap-1">
            <CornerDownRightIcon
              aria-hidden="true"
              className="-ml-3 size-3 shrink-0 text-muted-foreground/60"
            />
            <Icon className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="shrink-0 truncate text-xs font-medium">{primary}</span>
            <span className="flex-1" />
            <SubagentStatusIndicator {...status} />
          </div>
          {child.last_message_preview && (
            <p className="truncate pl-[22px] text-[11px] text-muted-foreground">
              {child.last_message_preview}
            </p>
          )}
        </Link>
      </li>
      {grandchildren.map((grandchild) => (
        <SubagentRow
          key={grandchild.id}
          child={grandchild}
          depth={depth + 1}
          conversationId={conversationId}
        />
      ))}
    </>
  );
}