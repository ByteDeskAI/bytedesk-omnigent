import { OfflineBanner } from "@/components/OfflineBanner";
import { supportsWindowControlsOverlay } from "@/lib/pwa/runtime";
import { isMacElectronShell } from "@/lib/nativeBridge";
import { Sidebar } from "../../Sidebar";
import { TitleBarServerPicker } from "../../TitleBarServerPicker";
import { AppShellChatRegion } from "./AppShellChatRegion";
import { AppShellModals } from "./AppShellModals";
import { AppShellPushPanels } from "./AppShellPushPanels";
import type { useAppShellState } from "./useAppShellState";

type AppShellLayoutProps = ReturnType<typeof useAppShellState>;

export function AppShellLayout(props: AppShellLayoutProps) {
  const { sidebarOpen, setSidebarOpen, activeConv, activeSession, setAgentInfoOpen } = props;

  return (
    <>
      <div
        className="app-shell relative flex h-dvh bg-sidebar text-foreground"
        data-electron-mac={isMacElectronShell() ? "true" : undefined}
        data-pwa-wco={supportsWindowControlsOverlay() ? "true" : undefined}
      >
        <OfflineBanner />
        {isMacElectronShell() && <div className="electron-drag-strip" aria-hidden="true" />}
        {isMacElectronShell() && (
          <TitleBarServerPicker threadTitle={activeSession?.title ?? activeConv?.title} />
        )}
        <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

        <div className="relative flex min-h-0 min-w-0 flex-1">
          <AppShellChatRegion {...props} setAgentInfoOpen={setAgentInfoOpen} />
          <AppShellPushPanels {...props} />
        </div>
      </div>
      <AppShellModals {...props} />
    </>
  );
}