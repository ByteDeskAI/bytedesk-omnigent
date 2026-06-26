import { Outlet, useLocation } from "@/lib/routing";

/**
 * Top-level route "section" for the page-entrance animation.
 *
 * Chat lives on both `/` and `/c/:conversationId`; collapsing them to one
 * section means switching conversations does NOT replay the entrance (the
 * ChatPage stays mounted and is out of scope for the polish pass). Every other
 * top-level route gets its own section, so navigating to it replays the fade.
 */
export function routeSection(pathname: string): string {
  const seg = pathname.split("/")[1] ?? "";
  return seg === "" || seg === "c" ? "chat" : seg;
}

/**
 * Wraps the routed page in a section-keyed container that replays the
 * `mc-page-enter` CSS animation whenever the top-level section changes.
 *
 * Pure CSS (token-driven, collapsed by the global reduced-motion gate in
 * index.css) — deliberately no animation library, matching the repo's
 * CSS-first motion idiom and adding zero bundle weight. The `key` forces a
 * remount of the wrapper on section change so the animation runs from the top.
 */
export function PageTransitionOutlet() {
  const { pathname } = useLocation();
  return (
    <div
      key={routeSection(pathname)}
      className="mc-page-enter flex min-h-0 min-w-0 flex-1 flex-col"
    >
      <Outlet />
    </div>
  );
}
