import type { ComponentType, SVGProps } from "react";
import {
  BookOpenIcon,
  Code2Icon,
  CompassIcon,
  FileTextIcon,
  FlaskConicalIcon,
  ScanSearchIcon,
  SearchIcon,
} from "lucide-react";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { GrokIcon } from "@/components/icons/GrokIcon";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import { type ChildSessionInfo } from "@/hooks/useChildSessions";
import type { SessionItem } from "@/lib/types";
import { nativeCodingAgentForWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import {
  CODEX_NATIVE_SUBAGENT_WRAPPER,
  MAIN_PREVIEW_MAX_CHARS,
  PI_AGENT_NAME,
  SESSION_SCOPED_PARAMS,
} from "./subagentsPanelConstants";

type AgentRowIcon = ComponentType<SVGProps<SVGSVGElement>>;

export type AgentActivity = "launching" | "working" | "awaiting" | "done" | "failed" | "idle" | "other";

export interface AgentStatus {
  activity: AgentActivity;
  label: string;
  details?: string;
}

export function railLinkSearch(search: string): string {
  const params = new URLSearchParams(search);
  for (const key of SESSION_SCOPED_PARAMS) params.delete(key);
  const next = params.toString();
  return next ? `?${next}` : "";
}

function firstErrorLine(message: string): string {
  const first = message
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean);
  return first ?? message;
}

export function childStatus(child: ChildSessionInfo): AgentStatus {
  if (child.pending_elicitations_count > 0) {
    return { activity: "awaiting", label: "Needs response" };
  }
  if (child.current_task_status === "launching") {
    return { activity: "launching", label: "Launching" };
  }
  if (child.busy) return { activity: "working", label: "Working" };
  if (child.last_task_error) {
    return {
      activity: "failed",
      label: "Failed",
      details: firstErrorLine(child.last_task_error.message),
    };
  }
  if (child.current_task_status === "failed") return { activity: "failed", label: "Failed" };
  if (child.current_task_status === "completed") return { activity: "done", label: "Done" };
  if (child.current_task_status) {
    return { activity: "other", label: child.current_task_status };
  }
  return { activity: "idle", label: "Idle" };
}

export function sessionStatus(status: string | undefined): AgentStatus {
  if (status === "launching") return { activity: "launching", label: "Launching" };
  if (status === "running") return { activity: "working", label: "Working" };
  if (status === "failed") return { activity: "failed", label: "Failed" };
  return { activity: "idle", label: "Idle" };
}

export const DOT_TONE: Record<Exclude<AgentActivity, "working" | "awaiting">, string> = {
  done: "bg-muted-foreground/55",
  failed: "bg-destructive",
  idle: "bg-muted-foreground/55",
  launching: "bg-muted-foreground/70",
  other: "bg-muted-foreground/55",
};

export const QUIET_STATE: Record<AgentActivity, boolean> = {
  launching: false,
  working: true,
  awaiting: false,
  failed: false,
  other: false,
  done: true,
  idle: true,
};

export const SETTLED_STATE: Record<AgentActivity, boolean> = {
  launching: false,
  working: false,
  awaiting: false,
  failed: false,
  other: false,
  done: true,
  idle: true,
};

export function iconForAgentType(tool: string | null): AgentRowIcon {
  const t = (tool ?? "").toLowerCase();
  if (t.includes("explore")) return SearchIcon;
  if (t.includes("research")) return BookOpenIcon;
  if (t.includes("plan") || t.includes("architect")) return CompassIcon;
  if (t.includes("review")) return ScanSearchIcon;
  if (t.includes("test")) return FlaskConicalIcon;
  if (t.includes("doc") || t.includes("writ")) return FileTextIcon;
  if (
    t.includes("code") ||
    t.includes("eng") ||
    t.includes("dev") ||
    t.includes("front") ||
    t.includes("back")
  ) {
    return Code2Icon;
  }
  return OttoIcon;
}

export function brandChildIcon(child: ChildSessionInfo): AgentRowIcon | null {
  const wrapper = child.labels?.[WRAPPER_LABEL_KEY];
  const nativeAgent = nativeCodingAgentForWrapper(wrapper);
  if (nativeAgent?.iconKind === "claude") return ClaudeIcon;
  if (nativeAgent?.iconKind === "codex") return CodexIcon;
  if (nativeAgent?.iconKind === "pi") return PiIcon;
  if (nativeAgent?.iconKind === "grok") return GrokIcon;
  if (child.tool === PI_AGENT_NAME) return PiIcon;
  return null;
}

export function childPrimaryLabel(child: ChildSessionInfo): string {
  const isUserAdded = child.title?.startsWith("ui:") ?? false;
  const isCodexNativeSubagent = child.labels?.[WRAPPER_LABEL_KEY] === CODEX_NATIVE_SUBAGENT_WRAPPER;
  if (isCodexNativeSubagent && !isUserAdded) {
    return child.tool ?? child.title ?? child.id;
  }
  let titleTask: string | null = null;
  if (child.title?.includes(":")) {
    const titleSuffix = child.title.split(":").slice(1).join(":");
    if (titleSuffix) titleTask = titleSuffix;
  }
  return child.session_name ?? titleTask ?? child.title ?? child.tool ?? child.id;
}

export function mainMessagePreview(items: SessionItem[] | undefined): string | null {
  if (!items) return null;
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.type !== "message") continue;
    const content = (item as { data?: { content?: unknown } }).data?.content;
    if (!Array.isArray(content)) continue;
    const text = content
      .map((block) =>
        block && typeof block === "object" && "text" in block
          ? String((block as { text: unknown }).text)
          : "",
      )
      .join("")
      .trim();
    if (text) {
      return text.length > MAIN_PREVIEW_MAX_CHARS
        ? `${text.slice(0, MAIN_PREVIEW_MAX_CHARS)}…`
        : text;
    }
  }
  return null;
}