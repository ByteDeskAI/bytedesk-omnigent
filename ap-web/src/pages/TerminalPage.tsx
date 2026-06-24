import { useCallback, useEffect, useState } from "react";
import { RefreshCwIcon, SquareTerminalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TerminalView } from "@/components/blocks/TerminalView";
import { getMe } from "@/lib/accountsApi";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getOmniCliTerminalStatus, type OmniCliTerminalStatus } from "@/lib/omniCliTerminalApi";
import { useNavigate } from "@/lib/routing";

export function TerminalPage() {
  const info = useServerInfo();
  const navigate = useNavigate();
  const [meIsAdmin, setMeIsAdmin] = useState<boolean | null>(null);
  const [status, setStatus] = useState<OmniCliTerminalStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoadError(null);
    const next = await getOmniCliTerminalStatus();
    if (next === null) {
      setLoadError("Could not load terminal status.");
      setStatus(null);
      return;
    }
    setStatus(next);
  }, []);

  useEffect(() => {
    void (async () => {
      if (info === "loading") return;
      if (!info.accounts_enabled) {
        setMeIsAdmin(true);
        await refresh();
        return;
      }
      const me = await getMe();
      if (me === null) {
        navigate("/login", { replace: true });
        return;
      }
      setMeIsAdmin(me.is_admin);
      if (me.is_admin) await refresh();
    })();
  }, [info, navigate, refresh]);

  if (meIsAdmin === null) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (!meIsAdmin) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Terminal</h1>
        <p className="text-sm text-muted-foreground">
          You don't have permission to open the terminal.
        </p>
      </div>
    );
  }

  const ready = status?.enabled === true && status.phase === "Running";

  return (
    <div className="flex min-h-full min-w-0 flex-col px-4 py-4 pt-14">
      <div className="mb-3 flex shrink-0 items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <SquareTerminalIcon className="size-5 shrink-0 text-muted-foreground" />
          <div className="min-w-0">
            <h1 className="truncate text-2xl font-semibold">Terminal</h1>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
              <span>{status?.pod_name ?? "omnigent-cli-0"}</span>
              <span>{status?.container ?? "cli"}</span>
              {status?.phase && <span>{status.phase}</span>}
            </div>
          </div>
        </div>
        <Button variant="ghost" size="icon" onClick={() => void refresh()} title="Refresh">
          <RefreshCwIcon />
        </Button>
      </div>

      {loadError !== null && (
        <div
          role="alert"
          className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {loadError}
        </div>
      )}

      {!ready && status !== null && (
        <div
          role="alert"
          className="mb-3 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground"
        >
          {status.enabled ? `Pod is ${status.phase ?? "unknown"}.` : "Terminal is disabled."}
        </div>
      )}

      <div className="min-h-[420px] flex-1 overflow-hidden rounded-md border border-border bg-background">
        {ready && <TerminalView attachPath={status.attach_path} />}
      </div>
    </div>
  );
}
