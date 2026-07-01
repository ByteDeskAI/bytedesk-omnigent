import { Loader2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ConnectionState } from "../TerminalSession";

export function StatusOverlay({
  state,
  reconnectPending,
  onResume,
  resumePending,
  resumeError,
}: {
  state: ConnectionState;
  reconnectPending: boolean;
  onResume?: () => void | Promise<void>;
  resumePending: boolean;
  resumeError: string | null;
}) {
  return (
    <div className="absolute inset-0 z-[10000] flex items-center justify-center bg-background/85 text-sm text-foreground backdrop-blur-[1px]">
      {state.kind === "connecting" && (
        <span className="flex items-center gap-2">
          <Loader2Icon className="size-4 animate-spin" />
          Connecting…
        </span>
      )}
      {state.kind === "closed" && reconnectPending && (
        <span data-testid="terminal-reconnecting" className="flex items-center gap-2">
          <Loader2Icon className="size-4 animate-spin" />
          Reconnecting…
        </span>
      )}
      {state.kind === "closed" && !reconnectPending && (
        <div className="flex flex-wrap items-center justify-center gap-2 px-3">
          <span>Bridge closed: {state.reason}</span>
          {onResume && (
            <Button
              type="button"
              size="xs"
              variant="secondary"
              onClick={onResume}
              disabled={resumePending}
              className="border-zinc-500/50 bg-zinc-100 text-zinc-950 hover:bg-white"
            >
              {resumePending ? "Resuming…" : "Resume session"}
            </Button>
          )}
          {resumeError && (
            <span className="basis-full text-center text-xs text-destructive">{resumeError}</span>
          )}
        </div>
      )}
      {state.kind === "error" && <span>Bridge error</span>}
    </div>
  );
}