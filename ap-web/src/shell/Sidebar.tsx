import {
  type CSSProperties,
  type MouseEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  InboxIcon,
  PanelRightOpenIcon,
  PencilIcon,
  SearchIcon,
} from "lucide-react";
import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { useConversations } from "@/hooks/useConversations";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useCommentInbox } from "@/hooks/useCommentInbox";
import { sumPendingApprovals } from "@/lib/inbox";
import { cn } from "@/lib/utils";
import { useResizableSidebar } from "@/hooks/useResizableSidebar";
import { ThemeModeMenu } from "@/components/theme/ThemeModeMenu";
import { PwaInstallButton } from "@/components/PwaInstallButton";
import { AccountMenu } from "./AccountMenu";
import { togglePinnedConversationId } from "./sidebarNav";
import { SidebarConversationList } from "./components/SidebarConversationList";
import { useActiveNavItem } from "./components/useActiveNavItem";
import { isMobileViewport } from "./components/sidebarViewport";
import {
  readPinnedConversationIds,
  writePinnedConversationIds,
} from "./components/sidebarStorage";

interface SidebarProps {
  open: boolean;
  onClose: () => void;
}

/**
 * Sidebar — brand mark, "New chat" button, conversations list.
 *
 * Responsive layout (mobile overlay vs desktop push) — see AppShell for
 * the layout side of the contract. Auto-close behavior is also
 * viewport-conditional:
 *
 *   - **Mobile**: navigation actions (New chat, conversation rows)
 *     close the sidebar. The sidebar covers the chat as a full-screen
 *     overlay, so dismissing on action is what reveals the new
 *     destination.
 *   - **Desktop**: navigation actions do NOT close. Only the X button
 *     in the brand row dismisses. Pushing chat content aside to read
 *     scrollback is fine; users typically want the conversations list
 *     to stay visible while they switch around.
 */
export function Sidebar({ open, onClose }: SidebarProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearchQuery, setDebouncedSearchQuery] = useState("");
  const [pinnedConversationIds, setPinnedConversationIds] = useState(readPinnedConversationIds);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearchQuery(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  const conversationsQuery = useConversations(debouncedSearchQuery, true, {
    reconcileWhileConnected: true,
  });

  const loadedRows = useMemo(
    () => (conversationsQuery.data?.pages ?? []).flatMap((page) => page.data),
    [conversationsQuery.data],
  );
  const pendingApprovals = useMemo(() => sumPendingApprovals(loadedRows), [loadedRows]);
  const unseenComments = useCommentInbox(loadedRows).items.length;
  const inboxCount = pendingApprovals + unseenComments;

  function onNavClick(e: MouseEvent<HTMLAnchorElement>) {
    if (e.defaultPrevented) return;
    if (e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (isMobileViewport()) onClose();
  }

  const { isNewChatPage, isInboxPage } = useActiveNavItem();

  useEffect(() => {
    writePinnedConversationIds(pinnedConversationIds);
  }, [pinnedConversationIds]);

  const togglePinnedConversation = useCallback((conversationId: string) => {
    setPinnedConversationIds((prev) => togglePinnedConversationId(prev, conversationId));
  }, []);

  const { width: sidebarWidth, handleProps: resizeHandleProps } = useResizableSidebar();

  return (
    <aside
      aria-label="Conversations"
      className={cn(
        "conversations-sidebar flex flex-col bg-card",
        "max-md:bg-card-solid",
        "fixed inset-0 z-50",
        open ? "translate-x-0" : "-translate-x-full",
        "md:relative md:inset-auto md:translate-x-0 md:overflow-hidden",
        open
          ? "md:m-2 md:w-[var(--sidebar-width)] md:rounded-xl md:border md:border-border md:shadow-lg"
          : "md:m-0 md:w-0 md:border-0",
      )}
      style={{ "--sidebar-width": `${sidebarWidth}px` } as CSSProperties}
      aria-hidden={!open}
      data-collapsed={!open || undefined}
      inert={open ? undefined : true}
    >
      <div
        {...resizeHandleProps}
        className="absolute inset-y-0 right-0 z-10 hidden w-1 cursor-col-resize transition-colors hover:bg-primary/30 active:bg-primary/50 md:block"
      />
      <div className="flex items-center justify-between px-4 pt-3">
        <Link
          to="/"
          onClick={onNavClick}
          className="rounded-sm text-[15px] font-semibold tracking-tight text-foreground transition-colors hover:text-foreground/70"
        >
          Omnigent
        </Link>
        <div className="flex items-center gap-1">
          <ThemeModeMenu />
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="Close sidebar"
                onClick={onClose}
                className="rounded-full"
              >
                <PanelRightOpenIcon className="size-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">Collapse sidebar</TooltipContent>
          </Tooltip>
        </div>
      </div>

      <div className="px-3 py-3">
        <Button
          asChild
          className={cn(
            "w-full justify-start gap-2 text-sm",
            isNewChatPage && "bg-muted font-semibold",
          )}
          variant="ghost"
          data-testid="new-chat-button"
        >
          <Link to="/" onClick={onNavClick}>
            <PencilIcon className="size-4 text-muted-foreground" />
            New session
          </Link>
        </Button>
        <Button
          asChild
          className={cn(
            "w-full justify-start gap-2 text-sm",
            isInboxPage && "bg-muted font-semibold",
          )}
          variant="ghost"
          data-testid="inbox-button"
        >
          <Link to="/inbox" onClick={onNavClick}>
            <InboxIcon className="size-4" />
            Inbox
            {inboxCount > 0 && (
              <span
                aria-label={
                  inboxCount === 1 ? "1 inbox item waiting" : `${inboxCount} inbox items waiting`
                }
                className="ml-auto inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-full bg-warning/15 px-1.5 text-[11px] font-medium text-warning tabular-nums"
              >
                {inboxCount}
              </span>
            )}
          </Link>
        </Button>
        <div className="relative mt-3">
          <SearchIcon className="-translate-y-1/2 pointer-events-none absolute top-1/2 left-2.5 size-3.5 text-muted-foreground" />
          <input
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            aria-label="Search sessions"
            placeholder="Search sessions"
            className="min-h-8 w-full rounded-full border border-input pr-3 pl-8 text-sm transition placeholder:text-muted-foreground focus-visible:outline-1"
          />
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto px-3 pb-3 [scrollbar-gutter:stable]">
        <SidebarConversationList
          conversationsQuery={conversationsQuery}
          onRowClick={onNavClick}
          searchQuery={debouncedSearchQuery}
          pinnedConversationIds={pinnedConversationIds}
          onPinnedConversationIdsChange={setPinnedConversationIds}
          onTogglePinned={togglePinnedConversation}
        />
      </nav>

      <div className="flex items-center gap-2 px-3 pb-2">
        <PwaInstallButton />
        <AccountMenu />
      </div>
    </aside>
  );
}