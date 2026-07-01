import {
  COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY,
  PINNED_CONVERSATION_IDS_STORAGE_KEY,
} from "../sidebarNav";

// Archived starts collapsed until the user touches any section header —
// once they do, the stored array (even an empty one) is the preference.
const DEFAULT_COLLAPSED_SIDEBAR_SECTIONS = ["Archived"];

export function readPinnedConversationIds(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((value): value is string => typeof value === "string");
  } catch {
    // Browser storage is user-editable and can contain stale/corrupt values.
    // Treat bad pin state as "no pins" instead of breaking navigation.
    return [];
  }
}

export function writePinnedConversationIds(ids: string[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(ids));
  } catch {
    // Pinning is a local navigation preference; storage failures should not
    // make the sidebar unusable.
  }
}

export function readCollapsedSidebarSections(): string[] {
  if (typeof window === "undefined") return DEFAULT_COLLAPSED_SIDEBAR_SECTIONS;
  try {
    const raw = window.localStorage.getItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY);
    if (!raw) return DEFAULT_COLLAPSED_SIDEBAR_SECTIONS;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return DEFAULT_COLLAPSED_SIDEBAR_SECTIONS;
    return parsed.filter((value): value is string => typeof value === "string");
  } catch {
    // Same contract as pins: corrupt storage means "back to defaults",
    // never a broken sidebar.
    return DEFAULT_COLLAPSED_SIDEBAR_SECTIONS;
  }
}

export function writeCollapsedSidebarSections(titles: string[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY, JSON.stringify(titles));
  } catch {
    // Collapse state is a local navigation preference; losing it is fine.
  }
}