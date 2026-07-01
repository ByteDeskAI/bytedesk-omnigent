import { type MouseEvent } from "react";
import { ChevronRightIcon } from "lucide-react";
import type { Conversation } from "@/hooks/useConversations";
import { cn } from "@/lib/utils";
import { SidebarConversationRow } from "./sidebar-conversation-row/SidebarConversationRow";

export function SidebarConversationSection({
  title,
  conversations,
  pinnedConversationIds,
  collapsedSections,
  onToggleCollapsed,
  onRowClick,
  onTogglePinned,
}: {
  title?: string;
  conversations: Conversation[];
  pinnedConversationIds: string[];
  collapsedSections: string[];
  onToggleCollapsed: (sectionTitle: string) => void;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
}) {
  const collapsed = title != null && collapsedSections.includes(title);
  return (
    <section>
      {title && (
        <h2>
          <button
            type="button"
            aria-expanded={!collapsed}
            onClick={() => onToggleCollapsed(title)}
            className="group flex w-full items-center gap-1 rounded-md px-2 py-1 text-left text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            {title}
            <ChevronRightIcon
              className={cn(
                "size-3.5 shrink-0 transition-transform",
                !collapsed && "rotate-90 opacity-0 group-hover:opacity-100",
              )}
            />
          </button>
        </h2>
      )}
      {!collapsed && (
        <ul className="flex flex-col gap-0.5">
          {conversations.map((conv) => (
            <SidebarConversationRow
              key={conv.id}
              conversation={conv}
              isPinned={pinnedConversationIds.includes(conv.id)}
              onClick={onRowClick}
              onTogglePinned={onTogglePinned}
            />
          ))}
        </ul>
      )}
    </section>
  );
}