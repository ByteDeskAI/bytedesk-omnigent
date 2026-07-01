// Right-side panel that shows the raw JSON items for a conversation,
// scoped to either the main thread or a sub-agent (child) session.
// Triggered from `SessionRail`'s execution-logs card.
//
// Mirrors `TerminalsPanel`'s layout contract (mobile overlay, desktop
// resizable push panel, Esc to close). Sub-agent session names can
// be long (``frontend_engineer · chat-panel-ascii-review-retry``), so
// the session selector is a Select dropdown rather than a horizontal
// tab strip — the dropdown fits its trigger to the panel width and
// lets long names truncate inline rather than wrap.
//
// JSON rendering is intentionally minimal: each item is rendered
// collapsed (one-line ``JSON.stringify``) by default and expands to a
// pretty-printed block on click. Items are numbered ``#1`` (oldest)
// through ``#N`` (newest) so users can see how many turns / items the
// session has accumulated.

import { XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useChildSessions } from "@/hooks/useChildSessions";
import { useResizablePanel } from "@/hooks/useResizablePanel";
import { cn } from "@/lib/utils";
import { buildLogEntries } from "./components/execution-logs/executionLogsUtils";
import { SessionItemsList } from "./components/execution-logs/SessionItemsList";

interface ExecutionLogsPanelProps {
  open: boolean;
  conversationId: string;
  initialKey: string | null;
  onClose: () => void;
}

export function ExecutionLogsPanel({
  open,
  conversationId,
  initialKey,
  onClose,
}: ExecutionLogsPanelProps) {
  const { children } = useChildSessions(open ? conversationId : null);
  const { panelWidth, handleProps, isDesktop } = useResizablePanel(open);
  const [activeKey, setActiveKey] = useState<string>("");
  const ref = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) {
      setActiveKey("");
      return;
    }
    if (initialKey) setActiveKey(initialKey);
  }, [open, initialKey]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (ref.current) {
      if (open) {
        ref.current.removeAttribute("inert");
      } else {
        ref.current.setAttribute("inert", "");
      }
    }
  }, [open]);

  const entries = buildLogEntries(conversationId, children);
  const activeEntry = entries.find((e) => e.key === activeKey) ?? entries[0] ?? null;

  return (
    <aside
      ref={ref}
      data-testid="execution-logs-panel"
      data-state={open ? "open" : "closed"}
      style={{ width: panelWidth }}
      className={cn(
        "flex flex-col overflow-hidden bg-card transition-[translate,border-color,border-width] duration-150 ease-out",
        "fixed inset-0 z-50 shadow-lg",
        open ? "translate-x-0" : "translate-x-full",
        "md:relative md:inset-auto md:z-auto md:shadow-none md:translate-x-0 md:shrink-0",
        open ? "md:border-border md:border-l" : "md:w-0 md:border-l-0",
      )}
      aria-hidden={!open}
      data-collapsed={!open || undefined}
    >
      {isDesktop && (
        <div
          {...handleProps}
          className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
        />
      )}
      <header className="flex shrink-0 items-center justify-between border-border border-b px-4 py-3">
        <h2 className="font-medium text-sm">Execution logs</h2>
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Close" onClick={onClose}>
          <XIcon className="size-4" />
        </Button>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-3 p-4">
        {!open || !activeEntry ? (
          <div className="flex-1" />
        ) : (
          <>
            <Select value={activeEntry.key} onValueChange={setActiveKey}>
              <SelectTrigger className="self-start">
                <SelectValue>
                  <span className="inline-flex items-center gap-2">
                    <activeEntry.icon className="size-3.5 shrink-0 text-muted-foreground" />
                    {activeEntry.label}
                  </span>
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {entries.map((e) => (
                  <SelectItem key={e.key} value={e.key}>
                    <span className="inline-flex items-center gap-2">
                      <e.icon className="size-3.5 shrink-0 text-muted-foreground" />
                      {e.label}
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <SessionItemsList key={activeEntry.sessionId} sessionId={activeEntry.sessionId} />
          </>
        )}
      </div>
    </aside>
  );
}