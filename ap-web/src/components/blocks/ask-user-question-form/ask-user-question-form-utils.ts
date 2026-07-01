import type { ClaudeQuestion } from "@/lib/askUserQuestion";

/**
 * Derive the final answer for a question given the current state.
 * Returns ``null`` when the question is unanswered.
 */
export function answerForQuestion(
  question: ClaudeQuestion,
  selection: string | string[],
  customSelected: boolean,
  customText: string,
): string | string[] | null {
  const customValue = customText.trim();
  if (question.multiSelect) {
    const selected = Array.isArray(selection) ? selection : [];
    const all =
      customSelected && customValue ? Array.from(new Set([...selected, customValue])) : selected;
    return all.length > 0 ? all : null;
  }
  if (customSelected) {
    return customValue || null;
  }
  return typeof selection === "string" && selection ? selection : null;
}

export function questionKey(question: ClaudeQuestion): string {
  return question.id && question.id.length > 0 ? question.id : question.question;
}