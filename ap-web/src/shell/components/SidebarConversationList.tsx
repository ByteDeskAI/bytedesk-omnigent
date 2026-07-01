import { type MouseEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Loader2Icon } from "lucide-react";
import { useParams } from "@/lib/routing";
import {
  type Conversation,
  useConversations,
  usePinnedConversationBackfill,
} from "@/hooks/useConversations";
import { useSessionSwitchHotkey } from "@/hooks/useSessionSwitchHotkey";
import {
  type ActiveChatOverride,
  computeNextActiveOverride,
  normalizePinnedConversationIds,
  sortByUpdatedAtDesc,
} from "../sidebarNav";
import { isOwnedByViewer, sameStringArray } from "./sidebarConversationConstants";
import { SidebarConversationSection } from "./SidebarConversationSection";
import {
  readCollapsedSidebarSections,
  writeCollapsedSidebarSections,
} from "./sidebarStorage";

interface SidebarConversationListProps {
  conversationsQuery: ReturnType<typeof useConversations>;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  searchQuery: string;
  pinnedConversationIds: string[];
  onPinnedConversationIdsChange: (ids: string[]) => void;
  onTogglePinned: (conversationId: string) => void;
}

export function SidebarConversationList({
  conversationsQuery,
  onRowClick,
  searchQuery,
  pinnedConversationIds,
  onPinnedConversationIdsChange,
  onTogglePinned,
}: SidebarConversationListProps) {
  const allConversations = useMemo(
    () => conversationsQuery.data?.pages.flatMap((page) => page.data) ?? [],
    [conversationsQuery.data],
  );

  const loadedIds = useMemo(() => new Set(allConversations.map((c) => c.id)), [allConversations]);
  const pinnedBackfill = usePinnedConversationBackfill(pinnedConversationIds, loadedIds);

  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  const [activeOverride, setActiveOverride] = useState<ActiveChatOverride | null>(null);
  useEffect(() => {
    setActiveOverride((prev) => computeNextActiveOverride(activeId, allConversations, prev));
  }, [activeId, allConversations]);

  const pinnedSet = useMemo(() => new Set(pinnedConversationIds), [pinnedConversationIds]);
  const sections = useMemo(() => {
    const allWithBackfill = [...allConversations, ...pinnedBackfill];
    const pinned = sortByUpdatedAtDesc(
      allWithBackfill.filter((c) => pinnedSet.has(c.id) && c.archived !== true),
      activeOverride,
    );
    const pinnedIdSet = new Set(pinned.map((c) => c.id));
    const active = allConversations.filter((c) => !pinnedIdSet.has(c.id) && c.archived !== true);
    const sessions = sortByUpdatedAtDesc(active.filter(isOwnedByViewer), activeOverride);
    const shared = sortByUpdatedAtDesc(
      active.filter((c) => !isOwnedByViewer(c)),
      activeOverride,
    );
    const archived = sortByUpdatedAtDesc(
      allWithBackfill.filter((c) => c.archived === true),
      activeOverride,
    );
    return { pinned, sessions, shared, archived };
  }, [allConversations, pinnedBackfill, pinnedSet, activeOverride]);

  const [collapsedSections, setCollapsedSections] = useState<string[]>(
    readCollapsedSidebarSections,
  );
  const toggleSectionCollapsed = useCallback((sectionTitle: string) => {
    setCollapsedSections((prev) => {
      const next = prev.includes(sectionTitle)
        ? prev.filter((t) => t !== sectionTitle)
        : [...prev, sectionTitle];
      writeCollapsedSidebarSections(next);
      return next;
    });
  }, []);

  const orderedConversationIds = useMemo(() => {
    const visible = (title: string, list: readonly Conversation[]) =>
      collapsedSections.includes(title) ? [] : list;
    return [
      ...visible("Pinned", sections.pinned),
      ...visible("Recent", sections.sessions),
      ...visible("Shared with me", sections.shared),
      ...visible("Archived", sections.archived),
    ].map((c) => c.id);
  }, [sections, collapsedSections]);
  useSessionSwitchHotkey(orderedConversationIds, activeId);

  const hasMorePages = conversationsQuery.hasNextPage;
  useEffect(() => {
    if (!conversationsQuery.data || hasMorePages || searchQuery) return;
    const allLoaded = [...allConversations, ...pinnedBackfill];
    const normalized = normalizePinnedConversationIds(pinnedConversationIds, allLoaded);
    if (!sameStringArray(normalized, pinnedConversationIds)) {
      onPinnedConversationIdsChange(normalized);
    }
  }, [
    conversationsQuery.data,
    hasMorePages,
    searchQuery,
    allConversations,
    pinnedBackfill,
    pinnedConversationIds,
    onPinnedConversationIdsChange,
  ]);

  if (conversationsQuery.isLoading) {
    return <p className="px-2 py-1 text-muted-foreground text-xs">Loading…</p>;
  }
  if (conversationsQuery.isError) {
    const err = conversationsQuery.error;
    return (
      <p className="px-2 py-1 text-destructive text-xs">
        Failed to load: {err instanceof Error ? err.message : String(err)}
      </p>
    );
  }
  const emptyMessage = searchQuery ? "No matching conversations" : "No active sessions";

  const totalVisible =
    sections.pinned.length +
    sections.sessions.length +
    sections.shared.length +
    sections.archived.length;

  return (
    <div className="flex flex-col gap-3">
      {totalVisible === 0 ? (
        <p className="px-2 py-1 text-muted-foreground text-xs">{emptyMessage}</p>
      ) : (
        <>
          {sections.pinned.length > 0 && (
            <SidebarConversationSection
              title="Pinned"
              conversations={sections.pinned}
              pinnedConversationIds={pinnedConversationIds}
              collapsedSections={collapsedSections}
              onToggleCollapsed={toggleSectionCollapsed}
              onRowClick={onRowClick}
              onTogglePinned={onTogglePinned}
            />
          )}
          {sections.sessions.length > 0 && (
            <SidebarConversationSection
              title="Recent"
              conversations={sections.sessions}
              pinnedConversationIds={pinnedConversationIds}
              collapsedSections={collapsedSections}
              onToggleCollapsed={toggleSectionCollapsed}
              onRowClick={onRowClick}
              onTogglePinned={onTogglePinned}
            />
          )}
          {sections.shared.length > 0 && (
            <SidebarConversationSection
              title="Shared with me"
              conversations={sections.shared}
              pinnedConversationIds={pinnedConversationIds}
              collapsedSections={collapsedSections}
              onToggleCollapsed={toggleSectionCollapsed}
              onRowClick={onRowClick}
              onTogglePinned={onTogglePinned}
            />
          )}
          {sections.archived.length > 0 && (
            <SidebarConversationSection
              title="Archived"
              conversations={sections.archived}
              pinnedConversationIds={pinnedConversationIds}
              collapsedSections={collapsedSections}
              onToggleCollapsed={toggleSectionCollapsed}
              onRowClick={onRowClick}
              onTogglePinned={onTogglePinned}
            />
          )}
          {hasMorePages && !collapsedSections.includes("Recent") && (
            <button
              type="button"
              disabled={conversationsQuery.isFetchingNextPage}
              onClick={() => {
                if (conversationsQuery.hasNextPage) void conversationsQuery.fetchNextPage();
              }}
              className="flex cursor-pointer items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-muted-foreground text-xs hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
            >
              {conversationsQuery.isFetchingNextPage ? (
                <>
                  <Loader2Icon className="size-3 animate-spin" />
                  Loading…
                </>
              ) : (
                "Load more"
              )}
            </button>
          )}
        </>
      )}
    </div>
  );
}