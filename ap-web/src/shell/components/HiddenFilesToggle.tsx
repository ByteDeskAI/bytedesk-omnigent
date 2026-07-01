import { EyeIcon, EyeOffIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export function HiddenFilesToggle({
  showHidden,
  onToggle,
  size,
  hiddenCount,
}: {
  showHidden: boolean;
  onToggle: () => void;
  size: "4" | "3.5";
  hiddenCount: number;
}) {
  const hasHidden = hiddenCount > 0 && !showHidden;
  const ariaLabel = showHidden ? "Hide hidden files" : "Show hidden files";
  const tooltipLabel = showHidden
    ? "Hide hidden files"
    : hasHidden
      ? `${hiddenCount} file${hiddenCount === 1 ? "" : "s"} in hidden directories. Click to show.`
      : "Show hidden files";
  const iconSize = size === "4" ? "size-4" : "size-3.5";
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-label={ariaLabel}
            className={cn(
              "cursor-pointer rounded p-1 hover:bg-muted",
              hasHidden
                ? "text-warning hover:text-warning/80"
                : "text-muted-foreground hover:text-foreground",
            )}
            onClick={onToggle}
          >
            {showHidden ? <EyeOffIcon className={iconSize} /> : <EyeIcon className={iconSize} />}
          </button>
        </TooltipTrigger>
        <TooltipContent side="bottom">{tooltipLabel}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}