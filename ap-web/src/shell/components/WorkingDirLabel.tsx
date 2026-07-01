import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

function dirBasename(path: string): string {
  return path.split(/[/\\]/).filter(Boolean).pop() ?? path;
}

export function WorkingDirLabel({ dir }: { dir: string }) {
  return (
    <span className="min-w-0 flex-1 flex items-center overflow-hidden">
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="inline-block max-w-full truncate font-mono text-[11px] text-muted-foreground cursor-default">
              {dirBasename(dir)}
            </span>
          </TooltipTrigger>
          <TooltipContent side="bottom">{dir}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    </span>
  );
}