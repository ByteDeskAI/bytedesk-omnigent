import { useState } from "react";
import { CopyIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * A read-only field paired with a one-click copy button.
 *
 * Used for both invite URLs and reset-issued passwords; both are
 * single-use sensitive values that need a frictionless copy path
 * since the user typically pastes them into Slack within seconds.
 */
export function CopyableValue({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // No clipboard permission — the input is still selectable.
    }
  };
  return (
    <div className="flex items-center gap-2">
      <Input
        value={value}
        readOnly
        className="font-mono text-xs"
        onFocus={(e) => e.currentTarget.select()}
      />
      <Button variant="outline" size="sm" onClick={() => void onCopy()} aria-label="Copy">
        <CopyIcon /> {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
}