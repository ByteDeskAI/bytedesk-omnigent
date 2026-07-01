import { useState } from "react";
import {
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import { CLAUDE_NATIVE_PERMISSION_MODES } from "./newChatLandingConstants";

export function NewChatPermissionModeOptions({
  value,
  onValueChange,
}: {
  value: string;
  onValueChange: (mode: string) => void;
}) {
  const [previewed, setPreviewed] = useState<string | null>(null);
  const detail = CLAUDE_NATIVE_PERMISSION_MODES.find(
    (m) => m.value === (previewed ?? value),
  )?.description;
  return (
    <>
      <DropdownMenuRadioGroup value={value} onValueChange={onValueChange}>
        {CLAUDE_NATIVE_PERMISSION_MODES.map((mode) => (
          <DropdownMenuRadioItem
            key={mode.value}
            value={mode.value}
            data-testid={`new-chat-landing-permission-${mode.value}`}
            onFocus={() => setPreviewed(mode.value)}
            onPointerEnter={() => setPreviewed(mode.value)}
            className="rounded-sm pl-2 py-1 text-xs"
          >
            {mode.label}
          </DropdownMenuRadioItem>
        ))}
      </DropdownMenuRadioGroup>
      <DropdownMenuSeparator />
      <p
        data-testid="new-chat-landing-permission-detail"
        className="min-h-5 px-2 pt-0.5 pb-1 text-xs leading-relaxed text-muted-foreground"
      >
        {detail}
      </p>
    </>
  );
}