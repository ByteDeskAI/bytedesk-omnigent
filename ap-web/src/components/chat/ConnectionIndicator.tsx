import { AlertTriangleIcon, Loader2Icon, WifiOffIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { SandboxStatus } from "@/lib/types";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import { useChatStore } from "@/store/chatStore";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import { CHAT_COLUMN_WIDTH } from "./chat-utils";
import { ConnectedTerminalFirstPill } from "./ConnectedTerminalFirstPill";

const SANDBOX_STAGE_LABELS: Record<string, string | undefined> = {
  provisioning: "Provisioning sandbox",
  cloning: "Cloning repository",
  starting: "Connecting host",
  connecting: "Starting agent",
};

export function SandboxFailedIndicator({ status }: { status: SandboxStatus }) {
  return (
    <div
      data-testid="sandbox-failed-indicator"
      role="status"
      className={cn(
        "mx-auto mb-4 flex w-full items-center justify-center gap-2 px-6 py-1.5 text-destructive text-xs",
        CHAT_COLUMN_WIDTH,
      )}
    >
      <AlertTriangleIcon className="size-3.5 shrink-0" aria-hidden />
      <span>Sandbox launch failed{status.error ? `: ${status.error}` : ""}</span>
    </div>
  );
}

export function ConnectionIndicator({
  liveness,
  onShowReconnectHelp,
}: {
  liveness: SessionLiveness;
  onShowReconnectHelp: () => void;
}) {
  const terminalFirst = useTerminalFirst();
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  if (sandboxStatus !== null) {
    if (sandboxStatus.stage === "failed") {
      return <SandboxFailedIndicator status={sandboxStatus} />;
    }
    return null;
  }
  const unreachable = liveness.kind === "host_offline" || liveness.kind === "local_stranded";
  if (unreachable) {
    return (
      <button
        type="button"
        data-testid="disconnected-indicator"
        onClick={onShowReconnectHelp}
        className={cn(
          "mx-auto mb-4 flex w-full items-center justify-center gap-2 px-6 py-1.5 text-xs text-destructive underline-offset-2 hover:underline",
          CHAT_COLUMN_WIDTH,
        )}
      >
        <WifiOffIcon className="size-3.5 shrink-0" />
        <span>
          {liveness.kind === "host_offline"
            ? "Host is offline — click to reconnect"
            : "Agent disconnected — click to reconnect"}
        </span>
      </button>
    );
  }

  if (terminalFirst?.isTerminalFirst) {
    if (terminalFirst.isShellView) return null;
    return <ConnectedTerminalFirstPill ctx={terminalFirst} />;
  }

  if (liveness.kind === "starting") {
    return (
      <div
        data-testid="connecting-indicator"
        className={cn(
          "mx-auto mb-4 flex w-full items-center justify-center gap-2 px-6 py-1.5 text-muted-foreground text-xs",
          CHAT_COLUMN_WIDTH,
        )}
      >
        <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
        <span>Connecting…</span>
      </div>
    );
  }

  return null;
}

export { SANDBOX_STAGE_LABELS };