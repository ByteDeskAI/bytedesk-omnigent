import { TriangleAlertIcon } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ConnectHostInstructions } from "./ConnectHostInstructions";
import { NewChatLandingComposer } from "./NewChatLandingComposer";
import { NewChatLandingHero } from "./NewChatLandingHero";
import { NewChatLandingFooterTray } from "./NewChatLandingFooterTray";
import { useNewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingScreen() {
  const state = useNewChatLandingState();

  return (
    <div className="flex flex-1 items-center justify-center" data-testid="new-chat-landing">
      <div className="flex w-full max-w-[840px] flex-col items-center gap-8 px-10 pt-8 pb-16">
        <NewChatLandingHero />
        <div className="relative flex w-full flex-col gap-3">
          <NewChatLandingComposer state={state} />
          <NewChatLandingFooterTray state={state} />
          {state.selectedAgentUnconfigured && (
            <p
              className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-500"
              data-testid="new-chat-landing-harness-warning"
            >
              <TriangleAlertIcon className="size-3.5 shrink-0" />
              <span>
                {state.selectedAgent?.display_name} isn&apos;t configured on {state.harnessWarningHost?.name} —
                run <code>omnigent setup</code> on that machine.
              </span>
            </p>
          )}
          {state.createError && (
            <p className="text-xs text-destructive" data-testid="new-chat-landing-error">
              {state.createError}
            </p>
          )}
        </div>
      </div>
      <Dialog open={state.connectOpen} onOpenChange={state.setConnectOpen}>
        <DialogContent className="sm:max-w-lg" data-testid="connect-host-dialog">
          <DialogHeader>
            <DialogTitle>Connect a host</DialogTitle>
          </DialogHeader>
          <ConnectHostInstructions
            serverUrl={state.serverUrl}
            label="Run this on the machine you want to use, then pick it from the host menu:"
          />
        </DialogContent>
      </Dialog>
    </div>
  );
}
