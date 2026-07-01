/**
 * Initial sidebar open state — open on desktop, closed on mobile. SSR-
 * safe (returns false when window is undefined). The threshold (`md`)
 * matches Tailwind's default 768px, used in the Sidebar's responsive
 * classes.
 */
export function initialSidebarOpen(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(min-width: 768px)").matches;
}