import { useLocation, useParams } from "@/lib/routing";

/**
 * Which top-level nav button (New session / Inbox) is active for the current
 * route.
 *
 * The inbox route has no param to key off, and the sidebar is basename-agnostic
 * (in embedded mode the routing seam rebases `to="/inbox"` → `${basename}/inbox`
 * behind its back), so `useMatch` / `NavLink` can't be used without knowing the
 * mount path. Instead compare the active route's last non-empty path segment,
 * which is `inbox` in both standalone and embedded modes. Conversation ids are
 * `conv_…`-prefixed, so a chat route's leaf can never collide with `inbox`.
 */
export function useActiveNavItem(): { isNewChatPage: boolean; isInboxPage: boolean } {
  const { conversationId: activeConversationId } = useParams<{ conversationId: string }>();
  const isInboxPage = useLocation().pathname.split("/").filter(Boolean).at(-1) === "inbox";
  // Exclude inbox: it also has no `:conversationId`, so it would otherwise
  // light up the "New session" button.
  const isNewChatPage = activeConversationId == null && !isInboxPage;
  return { isNewChatPage, isInboxPage };
}