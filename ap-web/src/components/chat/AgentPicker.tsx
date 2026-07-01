import { useEffect, useRef, useState } from "react";
import { ChevronDownIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { agentDisplayLabel } from "@/components/AgentInfo";
import { BRAIN_HARNESS_LABELS } from "@/lib/agentLabels";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import { cn } from "@/lib/utils";
import type { Agent } from "@/hooks/useAgents";
import { useChatStore } from "@/store/chatStore";
import { formatEffortLabel, isModelImplicitlySelected } from "./chat-utils";

interface AgentPickerProps {
  agents: Agent[] | undefined;
  isLoading: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  effortLevels: readonly string[];
  showEffort: boolean;
  showModels: boolean;
  disabled?: boolean;
  openNonce?: number;
}

function PickerSectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-2 pt-2 pb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
      {children}
    </div>
  );
}

export function AgentPicker({
  agents,
  isLoading,
  selectedId,
  onSelect,
  effortLevels,
  showEffort,
  showModels,
  disabled = false,
  openNonce = 0,
}: AgentPickerProps) {
  const [open, setOpen] = useState(false);
  const appliedOpenNonce = useRef(0);
  useEffect(() => {
    if (!openNonce || openNonce === appliedOpenNonce.current) return;
    appliedOpenNonce.current = openNonce;
    setOpen(true);
  }, [openNonce]);

  const hasAgents = !!agents && agents.length > 0;
  const selectedEffort = useChatStore((s) => s.selectedEffort);
  const selectedModel = useChatStore((s) => s.selectedModel);
  const llmModel = useChatStore((s) => s.llmModel);

  const isClaudeNative = showModels;
  const showAgents = !isClaudeNative && (agents?.length ?? 0) > 1;
  const selectedAgent = agents?.find((a) => a.id === selectedId) ?? agents?.[0];
  const agentDisplayName = selectedAgent
    ? (selectedAgent.display_name ?? agentDisplayLabel(selectedAgent.name))
    : undefined;
  const sessionHarness = useChatStore((s) => s.sessionHarness);
  const harnessLabel = sessionHarness ? (BRAIN_HARNESS_LABELS[sessionHarness] ?? null) : null;

  const effortLabel = showEffort && selectedEffort ? formatEffortLabel(selectedEffort) : null;
  const hasPickerActions = showAgents || showModels || showEffort;

  let triggerLabel: string;
  if (isLoading) {
    triggerLabel = "Loading…";
  } else if (!hasAgents) {
    triggerLabel = "No agents";
  } else if (isClaudeNative) {
    triggerLabel = "Claude";
  } else {
    const nameWithHarness =
      agentDisplayName && harnessLabel ? `${agentDisplayName} (${harnessLabel})` : agentDisplayName;
    const parts = [nameWithHarness, effortLabel].filter(
      (p): p is string => p != null && p.length > 0,
    );
    triggerLabel = parts.join(" · ");
  }

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={!hasAgents || disabled || !hasPickerActions}
          data-testid="agent-picker-trigger"
          className="h-7 min-w-0 shrink gap-1.5 px-2 text-muted-foreground hover:text-foreground"
        >
          <span className="min-w-0 truncate text-xs tabular-nums">{triggerLabel}</span>
          {hasPickerActions && <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-64 p-1">
        {showAgents && (
          <>
            <PickerSectionHeader>Agents</PickerSectionHeader>
            {agents?.map((a) => (
              <DropdownMenuItem
                key={a.id}
                data-testid="agent-picker-item"
                data-agent-id={a.id}
                data-agent-name={a.name}
                data-active={a.id === selectedId ? "true" : undefined}
                onSelect={() => onSelect(a.id)}
                className={cn(
                  "items-start gap-2 rounded-sm px-2 py-1.5 text-xs",
                  "data-[active=true]:bg-accent/60 data-[active=true]:text-foreground",
                )}
              >
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="truncate">{a.display_name ?? agentDisplayLabel(a.name)}</span>
                  {a.description && (
                    <span className="truncate text-xs text-muted-foreground">{a.description}</span>
                  )}
                </div>
              </DropdownMenuItem>
            ))}
          </>
        )}
        {showModels && (
          <>
            {!isClaudeNative && <DropdownMenuSeparator className="my-1" />}
            <PickerSectionHeader>Models</PickerSectionHeader>
            {CLAUDE_NATIVE_MODELS.map((m) => {
              const isExplicit = selectedModel === m.id;
              const isImplicit =
                selectedModel === null && isModelImplicitlySelected(m.id, llmModel);
              const isActive = isExplicit || isImplicit;
              return (
                <DropdownMenuItem
                  key={m.id}
                  data-testid="model-picker-item"
                  data-model-id={m.id}
                  data-active={isActive ? "true" : undefined}
                  onSelect={() =>
                    void useChatStore
                      .getState()
                      .setModel(m.id)
                      .catch(() => {})
                  }
                  className={cn(
                    "items-center gap-2 rounded-sm px-2 py-1.5 text-xs",
                    "data-[active=true]:bg-accent/60 data-[active=true]:text-foreground",
                  )}
                >
                  <span className="flex-1 truncate">{m.label}</span>
                </DropdownMenuItem>
              );
            })}
          </>
        )}
        {showEffort && (
          <>
            {(showAgents || showModels) && <DropdownMenuSeparator className="my-1" />}
            <PickerSectionHeader>Effort</PickerSectionHeader>
            {effortLevels.map((level) => (
              <DropdownMenuItem
                key={level}
                data-testid="effort-picker-item"
                data-effort-level={level}
                data-active={selectedEffort === level ? "true" : undefined}
                onSelect={() =>
                  void useChatStore
                    .getState()
                    .setEffort(level)
                    .catch(() => {})
                }
                className={cn(
                  "items-center gap-2 rounded-sm px-2 py-1.5 text-xs capitalize",
                  "data-[active=true]:bg-accent/60 data-[active=true]:text-foreground",
                )}
              >
                <span className="flex-1 truncate">{level}</span>
              </DropdownMenuItem>
            ))}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}