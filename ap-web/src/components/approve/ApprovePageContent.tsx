import { CheckIcon, MessageCircleQuestionMark, XIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { formatPreview } from "@/lib/previewFormat";
import type { ApprovePageState } from "./approve-types";

export function ApprovePageContent({
  state,
  onSubmit,
}: {
  state: ApprovePageState;
  onSubmit: (action: "accept" | "decline") => void;
}) {
  return (
    <div className="mx-auto flex min-h-screen max-w-xl items-center justify-center p-6">
      {state.kind === "loading" && (
        <Alert className="flex flex-col gap-2 py-4 px-5">
          <AlertTitle className="text-sm">Loading elicitation…</AlertTitle>
        </Alert>
      )}

      {state.kind === "resolved" && (
        <Alert className="flex flex-col gap-2 border-muted py-4 px-5">
          <AlertTitle className="text-sm">Elicitation resolved</AlertTitle>
          <AlertDescription className="text-xs">
            This approval request is no longer pending. It may have been resolved, timed out, or
            cancelled.
          </AlertDescription>
        </Alert>
      )}

      {state.kind === "error" && (
        <Alert variant="destructive" className="flex flex-col gap-2 py-4 px-5">
          <AlertTitle className="text-sm">Error</AlertTitle>
          <AlertDescription className="text-xs">{state.message}</AlertDescription>
        </Alert>
      )}

      {state.kind === "submitted" && (
        <Alert className="flex flex-col gap-1 border-muted py-4 px-5">
          <AlertTitle className="flex items-center gap-2 text-sm">
            {state.action === "accept" ? (
              <>
                <CheckIcon className="size-4 text-success" />
                Approved
              </>
            ) : (
              <>
                <XIcon className="size-4 text-destructive" />
                Rejected
              </>
            )}
          </AlertTitle>
          <AlertDescription className="text-xs">You can close this page.</AlertDescription>
        </Alert>
      )}

      {state.kind === "pending" && (
        <Alert className="flex flex-col gap-3 py-4 px-5">
          <AlertTitle className="flex items-center gap-2 text-sm">
            <MessageCircleQuestionMark className="size-4 text-yellow-600 dark:text-yellow-400" />
            Approval required
            {state.data.policy_name && (
              <span className="text-muted-foreground text-xs">· {state.data.policy_name}</span>
            )}
            {state.data.phase && (
              <span className="text-muted-foreground text-xs">({state.data.phase})</span>
            )}
          </AlertTitle>
          <AlertDescription className="flex flex-col gap-2">
            <span>{state.data.message}</span>
            {state.data.content_preview && (
              <pre className="max-h-64 overflow-y-auto rounded bg-muted px-2 py-1 font-mono text-xs whitespace-pre-wrap break-words">
                {formatPreview(state.data.content_preview)}
              </pre>
            )}
            <div className="flex flex-wrap gap-2 pt-1">
              <Button size="sm" onClick={() => onSubmit("accept")}>
                <CheckIcon className="mr-1 size-3.5" />
                Approve
              </Button>
              <Button size="sm" variant="outline" onClick={() => onSubmit("decline")}>
                <XIcon className="mr-1 size-3.5" />
                Reject
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}