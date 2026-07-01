import type { Conversation } from "@/hooks/useConversations";
import { isOwnerLevel } from "@/lib/permissionsApi";

// Positioning shared by both occupants of a row's trailing time-marker slot
// (the session-state badge or the relative timestamp). On desktop the slot
// fades out on hover/focus so the pin + kebab controls can take its place;
// on mobile it sits left of the always-visible controls (right-[4.5rem]).
export const TIME_MARKER_SLOT_CLASS =
  "-translate-y-1/2 pointer-events-none absolute top-1/2 right-[4.5rem] flex h-5 items-center transition-opacity md:right-2 md:group-hover:opacity-0 md:group-has-[:focus-visible]:opacity-0 md:group-has-[[aria-expanded=true]]:opacity-0";

export function sameStringArray(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

// permission_level null (no ACL row / legacy) or >= 4 both mean owner.
export function isOwnedByViewer(conversation: Conversation): boolean {
  return isOwnerLevel(conversation.permission_level);
}