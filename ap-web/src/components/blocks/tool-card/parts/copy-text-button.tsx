import { CheckIcon, CopyIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export interface CopyTextButtonProps {
  text: string;
  label: string;
}

export function CopyTextButton({ text, label }: CopyTextButtonProps) {
  const [isCopied, setIsCopied] = useState(false);
  const timeoutRef = useRef<number | null>(null);

  const copyToClipboard = useCallback(async () => {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) {
      return;
    }

    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return;
    }

    setIsCopied(true);
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current);
    }
    timeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
  }, [text]);

  useEffect(
    () => () => {
      if (timeoutRef.current !== null) {
        window.clearTimeout(timeoutRef.current);
      }
    },
    [],
  );

  const Icon = isCopied ? CheckIcon : CopyIcon;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={isCopied ? "Copied" : label}
          className="size-6 text-muted-foreground"
          onClick={copyToClipboard}
          size="icon-xs"
          type="button"
          variant="ghost"
        >
          <Icon className="size-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{isCopied ? "Copied" : label}</TooltipContent>
    </Tooltip>
  );
}