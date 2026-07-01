/**
 * Map from question id/text → either a single selected label
 * (single-select) or a list of labels (multi-select).
 */
export type AskUserQuestionAnswers = Record<string, string | string[]>;