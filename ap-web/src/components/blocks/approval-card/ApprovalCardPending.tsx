import {
  ClipboardListIcon,
  ExternalLinkIcon,
  MessageCircleQuestionMark,
  TerminalIcon,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { AskUserQuestionPayload } from "@/lib/askUserQuestion";
import { AskUserQuestionForm } from "../ask-user-question-form";
import { ExitPlanModeReview } from "../ExitPlanModeReview";
import { BinaryApprovalActions, CodexCommandActions } from "./ApprovalCardActions";
import type { ApprovalCardProps, ApprovalHandlers, ApprovalMode } from "./types";

export function ApprovalCardPending({
  message,
  phase,
  policyName,
  url,
  codexCommand,
  allowAllEdits,
  askPayload,
  mode,
  handlers,
}: Pick<
  ApprovalCardProps,
  "message" | "phase" | "policyName" | "url" | "codexCommand" | "allowAllEdits"
> & {
  askPayload: AskUserQuestionPayload | null;
  mode: ApprovalMode;
  handlers: ApprovalHandlers;
}) {
  const {
    isExitPlanMode,
    exitPlanModePlan,
    isAskUserQuestion,
    isMultiChoice,
    isCodexCommandApproval,
    isExternalUrl,
    askUserQuestionTitle,
    formattedPreview,
    optionLabels,
    execPolicyAmendment,
  } = mode;

  return (
    <Alert
      data-testid="approval-card"
      data-state="pending"
      className="flex flex-col gap-2 py-3 px-4"
    >
      <AlertTitle className="flex items-center gap-2 text-sm">
        {isCodexCommandApproval ? (
          <TerminalIcon className="size-4 text-yellow-600 dark:text-yellow-400" />
        ) : isExitPlanMode ? (
          <ClipboardListIcon className="size-4 text-yellow-600 dark:text-yellow-400" />
        ) : (
          <MessageCircleQuestionMark className="size-4 text-yellow-600 dark:text-yellow-400" />
        )}
        {isCodexCommandApproval
          ? "Command approval"
          : isExitPlanMode
            ? "Plan review"
            : isAskUserQuestion
              ? askUserQuestionTitle
              : isMultiChoice
                ? "Choose an option"
                : "Approval required"}
        {policyName && !isAskUserQuestion && !isExitPlanMode && (
          <span className="text-muted-foreground text-xs">· {policyName}</span>
        )}
        {phase && !isMultiChoice && !isAskUserQuestion && !isExitPlanMode && (
          <span className="text-muted-foreground text-xs">({phase})</span>
        )}
      </AlertTitle>
      <AlertDescription className="flex flex-col gap-2">
        {isExitPlanMode && exitPlanModePlan ? (
          <>
            <span>Claude finished planning and wants to proceed.</span>
            <ExitPlanModeReview
              plan={exitPlanModePlan}
              onAcceptAuto={handlers.submitAllowAllEdits}
              onAccept={() => handlers.submitBinary("accept")}
              onReject={handlers.submitPlanRejection}
            />
          </>
        ) : isAskUserQuestion && askPayload ? (
          <AskUserQuestionForm
            questions={askPayload.questions}
            onSubmit={handlers.submitAnswers}
            onReject={() => handlers.submitBinary("decline")}
          />
        ) : isCodexCommandApproval && codexCommand ? (
          <>
            <span>Codex wants to run this command.</span>
            {codexCommand.reason && <span className="text-foreground">{codexCommand.reason}</span>}
            <pre className="overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-xs text-foreground whitespace-pre-wrap">
              {codexCommand.command}
            </pre>
            {codexCommand.cwd && (
              <span className="text-xs">
                cwd:{" "}
                <code className="rounded bg-muted px-1 py-0.5 font-mono">{codexCommand.cwd}</code>
              </span>
            )}
            <CodexCommandActions
              execPolicyAmendment={execPolicyAmendment}
              onAccept={() => handlers.submitBinary("accept")}
              onDecline={() => handlers.submitBinary("decline")}
              onApproveAndRemember={handlers.submitExecPolicyAmendment}
            />
          </>
        ) : (
          <>
            <span>{message}</span>
            {formattedPreview && (
              <pre className="max-h-64 overflow-y-auto rounded bg-muted px-2 py-1 font-mono text-xs whitespace-pre-wrap break-words">
                {formattedPreview}
              </pre>
            )}
            {isExternalUrl ? (
              <div className="flex flex-wrap gap-2 pt-1">
                <Button size="sm" asChild>
                  <a href={url!} target="_blank" rel="noopener noreferrer">
                    <ExternalLinkIcon className="mr-1 size-3.5" />
                    Open approval page
                  </a>
                </Button>
              </div>
            ) : isMultiChoice ? (
              <div className="flex flex-wrap gap-2 pt-1" data-testid="approval-card-options">
                {optionLabels.map((optLabel) => (
                  <Button
                    key={optLabel}
                    size="sm"
                    variant="outline"
                    onClick={() => handlers.submitOption(optLabel)}
                  >
                    {optLabel}
                  </Button>
                ))}
              </div>
            ) : (
              <BinaryApprovalActions
                allowAllEdits={allowAllEdits}
                onAccept={() => handlers.submitBinary("accept")}
                onDecline={() => handlers.submitBinary("decline")}
                onAcceptAllowAllEdits={handlers.submitAllowAllEdits}
              />
            )}
          </>
        )}
      </AlertDescription>
    </Alert>
  );
}