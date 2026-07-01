import {
  type AskUserQuestionPayload,
  castAskUserQuestionPayload,
  parseAskUserQuestionPreview,
} from "@/lib/askUserQuestion";
import { formatPreview } from "@/lib/previewFormat";
import type { ApprovalCardProps, ApprovalMode } from "./types";

/**
 * Extract the answer-option labels from an AskUserQuestion-shaped
 * ``requestedSchema``. Returns an empty array for any other schema.
 *
 * Currently unused for built-in AskUserQuestion (which routes
 * through PermissionRequest with a content_preview rather than a
 * structured schema), but kept for MCP-elicitation paths that may
 * emit this shape.
 */
export function extractOptionLabels(schema: Record<string, unknown>): string[] {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object") return [];
  const answer = (properties as Record<string, unknown>).answer;
  if (!answer || typeof answer !== "object") return [];
  const enumValues = (answer as Record<string, unknown>).enum;
  if (!Array.isArray(enumValues)) return [];
  return enumValues.filter((v): v is string => typeof v === "string" && v.length > 0);
}

export function deriveApprovalMode(props: ApprovalCardProps): {
  askPayload: AskUserQuestionPayload | null;
  mode: ApprovalMode;
} {
  const {
    contentPreview,
    requestedSchema,
    url,
    policyName,
    phase,
    codexCommand,
    askUserQuestion,
    exitPlanMode,
  } = props;

  const askPayload: AskUserQuestionPayload | null =
    castAskUserQuestionPayload(askUserQuestion) ?? parseAskUserQuestionPreview(contentPreview);

  const exitPlanModePlan =
    exitPlanMode && typeof exitPlanMode.plan === "string" && exitPlanMode.plan
      ? exitPlanMode.plan
      : null;
  const isExitPlanMode = exitPlanModePlan !== null;
  const optionLabels = askPayload === null ? extractOptionLabels(requestedSchema) : [];
  const isAskUserQuestion = askPayload !== null;
  const isMultiChoice = optionLabels.length > 0;
  const isCodexCommandApproval = codexCommand !== null && codexCommand !== undefined;
  const isExternalUrl =
    typeof url === "string" && url.length > 0 && !url.startsWith("/approve/");
  const askUserQuestionTitle =
    policyName.startsWith("codex_") || phase.startsWith("codex_")
      ? "Codex needs input"
      : "Claude has questions";

  const formattedPreview =
    isAskUserQuestion || isExitPlanMode || isMultiChoice || isCodexCommandApproval
      ? ""
      : formatPreview(contentPreview);
  const execPolicyAmendment =
    codexCommand?.execPolicyAmendment && codexCommand.execPolicyAmendment.length > 0
      ? codexCommand.execPolicyAmendment
      : null;

  return {
    askPayload,
    mode: {
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
    },
  };
}