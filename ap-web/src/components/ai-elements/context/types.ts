import type { LanguageModelUsage } from "ai";

export type ModelId = string;

export interface ContextSchema {
  usedTokens: number;
  maxTokens: number;
  usage?: LanguageModelUsage;
  modelId?: ModelId;
}