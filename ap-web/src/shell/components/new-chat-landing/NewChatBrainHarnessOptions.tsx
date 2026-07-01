import { Badge } from "@/components/ui/badge";
import { DropdownMenuRadioGroup, DropdownMenuRadioItem } from "@/components/ui/dropdown-menu";
import { BRAIN_HARNESS_LABELS } from "@/lib/agentLabels";
import type { Host } from "@/hooks/useHosts";
import { harnessUnconfiguredOnHost } from "./newChatLandingUtils";

export function NewChatBrainHarnessOptions({
  value,
  onValueChange,
  host,
}: {
  value: string;
  onValueChange: (harness: string) => void;
  host: Host | undefined | null;
}) {
  return (
    <>
      <div className="px-2 pt-1.5 pb-0.5 text-[11px] font-medium text-muted-foreground">
        Agent Harness
      </div>
      <DropdownMenuRadioGroup value={value} onValueChange={onValueChange}>
        {Object.entries(BRAIN_HARNESS_LABELS).map(([id, label]) => (
          <DropdownMenuRadioItem
            key={id}
            value={id}
            data-testid={`new-chat-landing-harness-${id}`}
            className="rounded-sm pl-2 py-1 text-xs"
          >
            <span className="flex-1">{label}</span>
            {harnessUnconfiguredOnHost(id, host) && (
              <Badge
                variant="outline"
                className="border-amber-300 bg-amber-50 text-[11px] text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-400"
                data-testid={`new-chat-landing-harness-warning-${id}`}
              >
                needs setup
              </Badge>
            )}
          </DropdownMenuRadioItem>
        ))}
      </DropdownMenuRadioGroup>
    </>
  );
}