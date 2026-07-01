import { FileViewerContext } from "./FileViewerContext";
import { TerminalFirstContextProvider } from "./TerminalFirstContext";
import { ForkDialogContextProvider } from "./ForkDialogContext";
import { AppShellLayout } from "./components/app-shell/AppShellLayout";
import { useAppShellState } from "./components/app-shell/useAppShellState";

/**
 * Top-level layout. The sidebar and right panels are responsive:
 *
 *   - **Mobile (`< md`)**: fixed full-screen overlays. When open they cover
 *     the chat with a translate-x slide-in. The sidebar's own X button
 *     dismisses (no backdrop — the overlay covers the viewport edge-to-edge,
 *     so there is no "outside" to click, and a `bg-black/20` layer behind
 *     it caused a persistent grey artifact at the iOS safe-area insets).
 *   - **Desktop (`md+`)**: static flex siblings. Open ↔ closed animates
 *     each panel's width, pushing the main content accordingly. No backdrop —
 *     side panels aren't covering anything.
 *
 * The right slot holds either `FilesPanel` (file tree) or `FileViewer`
 * (code + comments) — never both at once. Selecting a file transitions
 * from the tree view to the code view in the same slot. The "← Back"
 * button in `FileViewer` returns to the file tree.
 *
 * Default open state is taken from the initial viewport: the left sidebar is
 * open on desktop and closed on mobile. The right files panel starts closed.
 *
 * **Mobile session-rail entry**: the desktop right column has no room on
 * a phone, so the rail's contents are reached via a top-right FAB that
 * opens a dropdown with "Files" and "Terminals" (the latter only when
 * one or more terminals exist). Each entry opens the matching push
 * panel — the FAB and the desktop rail cards route through the same
 * open*() handlers.
 *
 * **Right rail tabs (desktop)**: the aside is internally tabbed between
 * Files, Terminals and Agents so each can claim the full rail height
 * instead of competing for a vertically-split slot. Files is the default;
 * within it a "Changed only" toggle filters the full folder tree down to
 * just the changed files (flat list). Opening a file (chat link or rail
 * click) forces the rail to the Files tab so the viewer is visible.
 * Terminal-first sessions render the terminal inline in main and therefore
 * hide the rail's Terminals tab. The Agents tab only appears once there's
 * more than one agent (the root has at least one child).
 */
export function AppShell() {
  const state = useAppShellState();

  return (
    <FileViewerContext.Provider value={state.fileViewerContextValue}>
      <TerminalFirstContextProvider value={state.terminalFirstContextValue}>
        <ForkDialogContextProvider value={state.forkDialogContextValue}>
          <AppShellLayout {...state} />
        </ForkDialogContextProvider>
      </TerminalFirstContextProvider>
    </FileViewerContext.Provider>
  );
}