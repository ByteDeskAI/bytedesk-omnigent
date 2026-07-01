import { ArrowUpIcon, PaperclipIcon, SquareIcon } from "lucide-react";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import {
  IntelligentModelControl,
  type CostControlMode,
  type CostRoutingVerdict,
} from "@/components/CostRoutingControl";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import type { Agent } from "@/hooks/useAgents";
import { AgentPicker } from "../AgentPicker";

export function ComposerActionRow({
  disabled,
  isReadOnly,
  hasPendingElicitation,
  showInterruptButton,
  hasDraft,
  costRoutingEligible,
  costControlModeOverride,
  costRoutingVerdict,
  agents,
  agentsLoading,
  selectedAgentId,
  onSelectAgent,
  effortLevels,
  showEffort,
  showModels,
  pickerOpenNonce,
  onAttachClick,
  onTranscript,
  onClearCommandError,
  resetCursor,
}: {
  disabled: boolean;
  isReadOnly: boolean;
  hasPendingElicitation: boolean;
  showInterruptButton: boolean;
  hasDraft: boolean;
  costRoutingEligible: boolean;
  costControlModeOverride: CostControlMode;
  costRoutingVerdict: CostRoutingVerdict | null;
  agents: Agent[] | undefined;
  agentsLoading: boolean;
  selectedAgentId: string | null;
  onSelectAgent: (id: string) => void;
  effortLevels: readonly string[];
  showEffort: boolean;
  showModels: boolean;
  pickerOpenNonce: number;
  onAttachClick: () => void;
  onTranscript: (text: string) => void;
  onClearCommandError: () => void;
  resetCursor: () => void;
}) {
  return (
    <div className="flex items-center justify-between gap-2 px-2 pb-2">
      <div className="flex shrink-0 items-center gap-0.5">
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="size-9 md:size-8"
          disabled={disabled || isReadOnly || hasPendingElicitation}
          onClick={onAttachClick}
          title="Attach files"
        >
          <PaperclipIcon className="size-4" />
          <span className="sr-only">Attach files</span>
        </Button>
        <ComposerMicButton
          disabled={disabled || isReadOnly || hasPendingElicitation}
          onTranscript={(text) => {
            onTranscript(text);
            resetCursor();
            onClearCommandError();
          }}
        />
      </div>
      <div className="flex min-w-0 items-center gap-0.5">
        {costRoutingEligible && (
          <IntelligentModelControl
            value={costControlModeOverride}
            onChange={(mode) =>
              void useChatStore
                .getState()
                .setCostControlMode(mode)
                .catch(() => {})
            }
            disabled={isReadOnly}
            verdict={costRoutingVerdict}
          />
        )}
        <AgentPicker
          agents={agents}
          isLoading={agentsLoading}
          selectedId={selectedAgentId}
          onSelect={onSelectAgent}
          effortLevels={effortLevels}
          showEffort={showEffort}
          showModels={showModels}
          disabled={isReadOnly}
          openNonce={pickerOpenNonce}
        />
        <Button
          type="submit"
          size="icon"
          variant={showInterruptButton ? "destructive" : "default"}
          className={cn(
            "size-9 shrink-0 rounded-full md:size-8",
            !showInterruptButton && "hover:bg-primary/90 disabled:opacity-30",
          )}
          disabled={
            showInterruptButton
              ? isReadOnly
              : !hasDraft || disabled || isReadOnly || hasPendingElicitation
          }
          title={showInterruptButton ? "Interrupt" : "Send"}
          aria-label={showInterruptButton ? "Interrupt" : "Send"}
        >
          {showInterruptButton ? (
            <SquareIcon className="size-4 fill-current" />
          ) : (
            <ArrowUpIcon className="size-4" />
          )}
          <span className="sr-only">{showInterruptButton ? "Interrupt" : "Send"}</span>
        </Button>
      </div>
    </div>
  );
}