import { Fragment } from "react";
import { ArrowUpIcon, ChevronDownIcon, PaperclipIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import { TIER_LABELS } from "@/lib/agentTiers";
import { NewChatAgentPickerRow } from "./NewChatAgentPickerRow";
import type { NewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingComposerActions({ state }: { state: NewChatLandingState }) {
  const s = state;

  return (
    <div className="flex items-center justify-between pt-1 pr-4 pb-3 pl-2">
      <div className="flex items-center gap-0.5">
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="size-9 md:size-8"
          disabled={s.creating}
          onClick={() => s.fileInputRef.current?.click()}
          title="Attach files"
          data-testid="new-chat-landing-attach"
        >
          <PaperclipIcon className="size-4" />
          <span className="sr-only">Attach files</span>
        </Button>
        <ComposerMicButton
          disabled={s.creating}
          onTranscript={(text) => s.setMessage((prev) => (prev ? `${prev} ${text}` : text))}
        />
      </div>
      <div className="flex items-center gap-0.5">
        {s.agentList.length > 0 ? (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                data-testid="new-chat-landing-agent-select"
                className="h-8 gap-1.5 px-2.5 text-muted-foreground hover:text-foreground"
              >
                <span className="max-w-20 truncate text-sm tabular-nums md:max-w-[18rem]">
                  {s.agentLabel}
                </span>
                <ChevronDownIcon className="size-3.5 opacity-60" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              side="bottom"
              className="max-h-[var(--radix-dropdown-menu-content-available-height)] min-w-64 max-w-[calc(100vw-2rem)] overflow-y-auto p-1"
            >
              {(["system", "harness", "employee", "workflow"] as const)
                .filter((tier) => s.agentTiers[tier].length > 0)
                .map((tier, index) => (
                  <Fragment key={tier}>
                    {index > 0 && <DropdownMenuSeparator />}
                    <div
                      className="px-2 pt-1.5 pb-0.5 text-[11px] font-medium text-muted-foreground"
                      data-testid={`new-chat-landing-agent-tier-${tier}`}
                    >
                      {TIER_LABELS[tier]}
                    </div>
                    {s.agentTiers[tier].map((agent) => (
                      <NewChatAgentPickerRow
                        key={agent.id}
                        agent={agent}
                        effectiveAgentId={s.effectiveAgentId}
                        harnessWarningHost={s.harnessWarningHost}
                        onPickHarnessClear={() => s.setPickedHarness(null)}
                        setPickedAgentId={s.setPickedAgentId}
                      />
                    ))}
                  </Fragment>
                ))}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : (
          <span className="text-xs text-muted-foreground">No agents</span>
        )}
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="inline-flex">
                <Button
                  type="submit"
                  size="icon"
                  disabled={!s.canSubmit}
                  aria-label="Start session"
                  data-testid="new-chat-landing-submit"
                  className="size-8 rounded-full bg-foreground text-card transition-opacity hover:opacity-80 disabled:opacity-50"
                >
                  <ArrowUpIcon className="size-4" />
                </Button>
              </span>
            </TooltipTrigger>
            {s.submitDisabledReason != null && (
              <TooltipContent>{s.submitDisabledReason}</TooltipContent>
            )}
          </Tooltip>
        </TooltipProvider>
      </div>
    </div>
  );
}