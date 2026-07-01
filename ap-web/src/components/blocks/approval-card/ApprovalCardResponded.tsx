import { CheckIcon, InfoIcon, XIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import type { ApprovalCardProps, ApprovalMode } from "./types";

export function ApprovalCardResponded({
  message,
  policyName,
  response,
  codexCommand,
  mode,
}: Pick<ApprovalCardProps, "message" | "policyName" | "response" | "codexCommand"> & {
  mode: ApprovalMode;
}) {
  if (!response) return null;

  const { isAskUserQuestion, isExitPlanMode, isCodexCommandApproval } = mode;
  const autoResolved = response.action === "auto_resolved";
  const accepted = response.action === "accept";

  const submittedAnswers =
    isAskUserQuestion && response.content && Object.keys(response.content).length > 0
      ? response.content
      : null;
  const selectedAnswer =
    !isAskUserQuestion && response.content && typeof response.content.answer === "string"
      ? (response.content.answer as string)
      : null;
  const planRejectionFeedback =
    isExitPlanMode &&
    response.action === "decline" &&
    typeof response.content?.feedback === "string" &&
    response.content.feedback
      ? response.content.feedback
      : null;
  const acceptedWithExecPolicy =
    Array.isArray(response.content?.execpolicy_amendment) &&
    response.content.execpolicy_amendment.every((entry) => typeof entry === "string");
  const acceptedAllEdits = response.content?.allow_all_edits === true;

  let icon = <XIcon className="size-4 text-destructive" />;
  let label = isExitPlanMode ? "Plan rejected" : "Rejected";
  if (autoResolved) {
    icon = <InfoIcon className="size-4 text-muted-foreground" />;
    label = "Resolved elsewhere";
  } else if (submittedAnswers !== null) {
    icon = <CheckIcon className="size-4 text-success" />;
    label = "Submitted";
  } else if (selectedAnswer !== null) {
    icon = <CheckIcon className="size-4 text-success" />;
    label = `Selected: ${selectedAnswer}`;
  } else if (acceptedWithExecPolicy) {
    icon = <CheckIcon className="size-4 text-success" />;
    label = "Approved and remembered";
  } else if (acceptedAllEdits) {
    icon = <CheckIcon className="size-4 text-success" />;
    label = isExitPlanMode ? "Plan approved · auto mode" : "Approved · auto-accepting edits";
  } else if (accepted) {
    icon = <CheckIcon className="size-4 text-success" />;
    label = isExitPlanMode ? "Plan approved" : "Approved";
  }

  return (
    <Alert
      data-testid="approval-card"
      data-state="responded"
      className="flex flex-col gap-1 border-muted"
    >
      <AlertTitle className="flex items-center gap-2 text-sm">
        {icon}
        {label}
        {policyName && <span className="text-muted-foreground text-xs">· {policyName}</span>}
      </AlertTitle>
      <AlertDescription className="flex flex-col gap-1 text-xs">
        {isCodexCommandApproval && codexCommand ? (
          <>
            {codexCommand.reason && <span>{codexCommand.reason}</span>}
            <pre className="overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-xs whitespace-pre-wrap">
              {codexCommand.command}
            </pre>
            {codexCommand.cwd && (
              <span>
                <span className="text-muted-foreground">cwd: </span>
                <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
                  {codexCommand.cwd}
                </code>
              </span>
            )}
          </>
        ) : (
          <span>{message}</span>
        )}
        {submittedAnswers !== null && (
          <ul className="flex flex-col gap-0.5 pl-3">
            {Object.entries(submittedAnswers).map(([q, ans]) => (
              <li key={q}>
                <span className="text-muted-foreground">{q}: </span>
                {Array.isArray(ans) ? ans.join(", ") : String(ans)}
              </li>
            ))}
          </ul>
        )}
        {planRejectionFeedback !== null && (
          <span className="italic" data-testid="plan-rejection-feedback">
            “{planRejectionFeedback}”
          </span>
        )}
      </AlertDescription>
    </Alert>
  );
}