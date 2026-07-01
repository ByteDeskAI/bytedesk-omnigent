import { InfoIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ForkSessionForm } from "./components/fork-session/ForkSessionForm";

export { ForkSessionForm } from "./components/fork-session/ForkSessionForm";

export function ForkSessionDialog({
  sourceSessionId,
  sourceTitle,
  sourceWorkspace,
  sourceHostId,
  sourceGitBranch,
  upToResponseId,
  open,
  onOpenChange,
}: {
  sourceSessionId: string;
  sourceTitle?: string | null;
  sourceWorkspace?: string | null;
  sourceHostId?: string | null;
  sourceGitBranch?: string | null;
  upToResponseId?: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const truncated = upToResponseId != null;
  const cloneDescription = `${
    truncated
      ? "Copies this session's history up to the selected response into a new session you own — messages after it aren't carried over"
      : "Copies this session's history into a new session you own"
  }${
    sourceWorkspace ? ", then starts it on the host and directory you pick" : ""
  }. Comments aren't copied, and changes in the clone won't affect the original.`;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="fork-session-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-1.5">
            {truncated ? "Fork from this response" : "Clone session"}
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  aria-label="What does cloning do?"
                  data-testid="fork-session-info"
                  tabIndex={-1}
                  className="cursor-pointer text-muted-foreground transition-colors hover:text-foreground"
                >
                  <InfoIcon className="size-4" />
                </button>
              </TooltipTrigger>
              <TooltipContent>{cloneDescription}</TooltipContent>
            </Tooltip>
          </DialogTitle>
          <DialogDescription className="sr-only">{cloneDescription}</DialogDescription>
        </DialogHeader>
        <ForkSessionForm
          sourceSessionId={sourceSessionId}
          sourceTitle={sourceTitle}
          sourceWorkspace={sourceWorkspace}
          sourceHostId={sourceHostId}
          sourceGitBranch={sourceGitBranch}
          upToResponseId={upToResponseId}
          onClose={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  );
}