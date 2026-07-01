import { ArrowRightIcon, ChevronDownIcon } from "lucide-react";
import { ApprovalCard, type SubmitApprovalFn } from "@/components/blocks/ApprovalCard";
import { Button } from "@/components/ui/button";
import type { InboxItem } from "@/lib/inbox";
import { relativeTime } from "@/lib/relativeTime";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel, getConversationAgentType } from "@/shell/sidebarNav";

type Verdict = { action: "accept" | "decline"; content?: Record<string, unknown> };

export function InboxApprovalItem({
  item,
  expanded,
  verdict,
  onToggleExpanded,
  onSubmit,
}: {
  item: InboxItem;
  expanded: boolean;
  verdict: Verdict | undefined;
  onToggleExpanded: () => void;
  onSubmit: SubmitApprovalFn;
}) {
  const elicitationId = item.elicitation.elicitationId;
  const title = conversationDisplayLabel(item.row);
  const agentLabel = getConversationAgentType(item.row);

  return (
    <div
      data-testid="inbox-item"
      data-expanded={expanded}
      className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4"
    >
      <div className="flex items-center gap-2">
        <button
          type="button"
          aria-expanded={expanded}
          onClick={onToggleExpanded}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <ChevronDownIcon
            className={cn(
              "size-4 shrink-0 text-muted-foreground transition-transform",
              !expanded && "-rotate-90",
            )}
          />
          <span className="min-w-0 shrink-0 truncate text-sm font-medium">
            {title}
            {agentLabel !== title && (
              <span className="ml-2 text-xs font-normal text-muted-foreground">{agentLabel}</span>
            )}
          </span>
          {!expanded && (
            <span className="min-w-0 truncate text-xs text-muted-foreground">
              {item.elicitation.message}
            </span>
          )}
        </button>
        <span className="flex shrink-0 items-center gap-2">
          <span className="text-xs text-muted-foreground">
            {relativeTime(item.row.updated_at * 1000)}
          </span>
          <Button asChild variant="ghost" size="sm" className="text-xs">
            <Link to={`/c/${item.row.id}`}>
              Open session
              <ArrowRightIcon className="ml-1 size-3.5" />
            </Link>
          </Button>
        </span>
      </div>
      {expanded && (
        <ApprovalCard
          elicitationId={elicitationId}
          message={item.elicitation.message}
          phase={item.elicitation.phase}
          policyName={item.elicitation.policyName}
          contentPreview={item.elicitation.contentPreview}
          requestedSchema={item.elicitation.requestedSchema}
          url={item.elicitation.url}
          status={verdict ? "responded" : "pending"}
          response={verdict ?? null}
          askUserQuestion={item.elicitation.askUserQuestion}
          exitPlanMode={item.elicitation.exitPlanMode}
          codexCommand={item.elicitation.codexCommand}
          allowAllEdits={item.elicitation.allowAllEdits}
          onSubmit={onSubmit}
        />
      )}
    </div>
  );
}