import { ShieldAlertIcon, ZapIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useChatStore } from "@/store/chatStore";
import { CockpitCard } from "./cockpit";

// The kill switch and arm are CONVERSATIONAL: there is no REST mutation —
// the goal-commander agent's `goal_set_posture` tool does it. These
// controls send a templated message to the commander session and the
// real posture is confirmed in the chat. The kill switch (set gated) is
// always allowed; arming full_auto is governance-gated (Wave 6, BDP-2599)
// and the commander refuses it until the founder-org switch is enabled.
const KILL_MESSAGE = "Set the goal-engine autonomy posture to gated (kill switch).";
const ARM_MESSAGE = "Arm the goal engine: set the autonomy posture to full_auto.";

export function AutonomyStrip({
  agentId,
  sessionId,
}: {
  agentId: string | null;
  sessionId: string | null;
}) {
  const ready = agentId !== null && sessionId !== null;

  function command(text: string) {
    if (!agentId) return;
    void useChatStore.getState().send(text, agentId);
  }

  return (
    <CockpitCard title="Autonomy" icon={<ShieldAlertIcon className="size-4" />}>
      <div className="space-y-3">
        <p className="text-xs text-muted-foreground">
          Posture is driven through the commander. The kill switch is always
          available; arming full-auto is governance-gated.
        </p>
        <Button
          variant="destructive"
          className="w-full"
          disabled={!ready}
          onClick={() => command(KILL_MESSAGE)}
        >
          <ShieldAlertIcon /> Kill switch — set gated
        </Button>
        <Button
          variant="outline"
          className="w-full"
          disabled={!ready}
          onClick={() => command(ARM_MESSAGE)}
        >
          <ZapIcon /> Arm full-auto
        </Button>
      </div>
    </CockpitCard>
  );
}
