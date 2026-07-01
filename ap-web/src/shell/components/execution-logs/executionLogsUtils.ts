import { BotIcon, MessageSquareIcon, type LucideIcon } from "lucide-react";
import {
  executionLogTabKey,
  MAIN_EXECUTION_LOG_KEY,
  type ChildSessionInfo,
} from "@/hooks/useChildSessions";
import type { RawSessionItem } from "@/hooks/useSessionItems";

export interface LogEntry {
  key: string;
  sessionId: string;
  label: string;
  icon: LucideIcon;
}

export function buildLogEntries(conversationId: string, children: ChildSessionInfo[]): LogEntry[] {
  const main: LogEntry = {
    key: executionLogTabKey(MAIN_EXECUTION_LOG_KEY),
    sessionId: conversationId,
    label: "main",
    icon: MessageSquareIcon,
  };
  const childEntries: LogEntry[] = children.map((c) => ({
    key: executionLogTabKey(c.id),
    sessionId: c.id,
    label: c.title ?? c.tool ?? c.id,
    icon: BotIcon,
  }));
  return [main, ...childEntries];
}

export function executionLogItemKey(item: RawSessionItem, idx: number): string {
  const id = item.id;
  return typeof id === "string" && id ? id : `idx-${idx}`;
}