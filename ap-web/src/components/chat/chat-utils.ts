import type { MessageContentBlock } from "@/lib/blocks";
import { parseSystemMessage } from "@/lib/systemMessage";
import { supportsEffortControl } from "@/lib/sessionCapabilities";
import { BUILTIN_SLASH_COMMANDS } from "@/components/SlashCommandMenu";
import type { Bubble, RenderItem } from "@/lib/renderItems";
import type { PendingInitialPrompt, PendingUserMessage } from "@/store/chatStore";
import type { Session, SessionStatus } from "@/lib/types";

/** All chat-column elements must share this width to stay aligned. */
export const CHAT_COLUMN_WIDTH = "max-w-3xl min-[1921px]:max-w-4xl min-[2561px]:max-w-5xl";

const ATTACHED_RE = /\[Attached:[^\]]*\]\s*/g;

export function extractUserText(content: MessageContentBlock[]): string {
  return content
    .filter(
      (c): c is Extract<MessageContentBlock, { type: "input_text" }> => c.type === "input_text",
    )
    .map((c) => c.text)
    .join("")
    .replace(ATTACHED_RE, "")
    .trim();
}

// Leading whitespace + the command token, so the composer overlay can tint
// just the `/skill` and leave any args in the default color.
const SLASH_COMMAND_SPLIT_RE = /^(\s*)(\/[A-Za-z0-9][\w:-]*)/;

/**
 * Split a slash-command draft into the command token and the rest, for the
 * composer highlight overlay. Returns null when the text isn't a command
 * (callers gate on `isSlashCommandText`, so a returned token is the full
 * command — never a `/etc/hosts`-style path prefix).
 */
export function splitSlashCommand(
  value: string,
): { before: string; token: string; after: string } | null {
  const m = SLASH_COMMAND_SPLIT_RE.exec(value);
  if (!m) return null;
  const [, before, token] = m;
  return { before, token, after: value.slice(before.length + token.length) };
}

/** Joins all `kind: "text"` items into a single markdown string for copying. */
export function collectBubbleMarkdown(items: RenderItem[]): string {
  return items
    .filter((item): item is Extract<RenderItem, { kind: "text" }> => item.kind === "text")
    .map((item) => item.text)
    .join("\n\n")
    .trim();
}

const TABLE_SEPARATOR_RE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/;

export function isMarkdownTableRow(line: string): boolean {
  return line.trim().includes("|");
}

export function containsMarkdownTable(items: RenderItem[]): boolean {
  return items.some((item) => {
    if (item.kind !== "text") return false;
    const lines = item.text.split("\n");
    return lines.some(
      (line, index) =>
        TABLE_SEPARATOR_RE.test(line) &&
        index > 0 &&
        index < lines.length - 1 &&
        isMarkdownTableRow(lines[index - 1] ?? "") &&
        isMarkdownTableRow(lines[index + 1] ?? ""),
    );
  });
}

/**
 * Build optimistic user bubbles from the pending-send queue.
 */
export function buildPendingBubbles(
  pending: PendingUserMessage[],
  selfAuthor: string | null,
): Bubble[] {
  return pending.map((p) => {
    const author = p.author ?? selfAuthor;
    return {
      kind: "user",
      itemId: p.tempId,
      content: p.content,
      ...(author !== null ? { createdBy: author } : {}),
    };
  });
}

export function isRequestElicitationBubble(bubble: Bubble): boolean {
  return (
    bubble.kind === "assistant" &&
    bubble.items.length > 0 &&
    bubble.items.every((it) => it.kind === "elicitation" && it.phase === "request")
  );
}

export function reorderCommittedRequestElicitations(committed: Bubble[]): Bubble[] {
  let result: Bubble[] | null = null;
  for (let i = 0; i < committed.length - 1; i += 1) {
    if (isRequestElicitationBubble(committed[i]!) && committed[i + 1]!.kind === "user") {
      if (result === null) result = [...committed];
      const card = result[i]!;
      result[i] = result[i + 1]!;
      result[i + 1] = card;
    }
  }
  return result ?? committed;
}

export function mergePendingBubbles(committed: Bubble[], pending: Bubble[]): Bubble[] {
  if (pending.length === 0) return committed;
  let insertAt = committed.length;
  while (insertAt > 0 && isRequestElicitationBubble(committed[insertAt - 1]!)) {
    insertAt -= 1;
  }
  if (insertAt === committed.length) return [...committed, ...pending];
  return [...committed.slice(0, insertAt), ...pending, ...committed.slice(insertAt)];
}

export function shouldShowAuthorBadge(
  author: string | undefined,
  viewerId: string | null,
  isSessionShared: boolean,
): boolean {
  return isSessionShared && author !== undefined && author !== viewerId;
}

export function isSessionSharedWithOthers(
  owner: string | null,
  viewerId: string | null,
  ownerGrants: readonly { user_id: string }[] | undefined,
): boolean {
  if (owner !== null && viewerId !== null && owner !== viewerId) return true;
  const viewerOwnsSession = owner !== null && owner === viewerId;
  return viewerOwnsSession && (ownerGrants ?? []).some((g) => g.user_id !== viewerId);
}

export function truncateTitle(raw: string, max = 60): string {
  const points = Array.from(raw);
  if (points.length <= max) return raw;
  const slice = points.slice(0, max - 1);
  const lastSpace = slice.lastIndexOf(" ");
  const cut = lastSpace > max - 10 ? lastSpace : slice.length;
  return slice.slice(0, cut).join("").trimEnd() + "…";
}

const SESSION_DRAFTS_KEY = "omnigent.sessionDrafts";

export function loadDraftsFromStorage(): Map<string, { text: string; files: File[] }> {
  try {
    const raw = window.sessionStorage.getItem(SESSION_DRAFTS_KEY);
    if (!raw) return new Map();
    const entries = JSON.parse(raw) as Record<string, string>;
    const map = new Map<string, { text: string; files: File[] }>();
    for (const [id, text] of Object.entries(entries)) {
      if (text) map.set(id, { text, files: [] });
    }
    return map;
  } catch {
    return new Map();
  }
}

export function saveDraftsToStorage(drafts: Map<string, { text: string; files: File[] }>): void {
  try {
    const obj: Record<string, string> = {};
    for (const [id, draft] of drafts) {
      if (draft.text) obj[id] = draft.text;
    }
    if (Object.keys(obj).length === 0) {
      window.sessionStorage.removeItem(SESSION_DRAFTS_KEY);
    } else {
      window.sessionStorage.setItem(SESSION_DRAFTS_KEY, JSON.stringify(obj));
    }
  } catch {
    // Storage full or unavailable — drafts still work in-memory.
  }
}

/** Per-session draft storage — survives Composer unmount during session switches. */
export const sessionDrafts = loadDraftsFromStorage();

/** Stable React key per bubble. */
export function bubbleKey(bubble: Bubble): string {
  if (bubble.kind === "user") return `user:${bubble.stableKey ?? bubble.itemId}`;
  if (bubble.kind === "compaction_loading") return `compaction_loading:${bubble.itemId}`;
  if (bubble.kind === "compaction") return `compaction:${bubble.itemId}`;
  return `assistant:${bubble.stableId}`;
}

export function hasInProgressAssistantBubble(bubbles: Bubble[]): boolean {
  return bubbles.some(
    (b) => b.kind === "assistant" && b.lifecycle === "streaming" && b.items.length > 0,
  );
}

export function shouldShowWorkingIndicator(showsWorking: boolean, bubbles: Bubble[]): boolean {
  if (!showsWorking) return false;
  if (hasInProgressAssistantBubble(bubbles)) return false;
  return bubbles[bubbles.length - 1]?.kind !== "compaction_loading";
}

export function isSystemBubble(bubble: Bubble): boolean {
  if (bubble.kind !== "user") return false;
  const hasAttachments = bubble.content.some(
    (c) => c.type === "input_image" || c.type === "input_file",
  );
  if (hasAttachments) return false;
  return parseSystemMessage(extractUserText(bubble.content)) !== null;
}

export function buildSlashCommandMap(
  skills: ReadonlyArray<{ name: string; description: string }>,
  showEffort: boolean,
  showModel: boolean,
): Record<string, string> {
  const m: Record<string, string> = {};
  for (const [name, description] of Object.entries(BUILTIN_SLASH_COMMANDS)) {
    if (name === "/effort" && !showEffort) continue;
    if (name === "/model" && !showModel) continue;
    m[name] = description;
  }
  for (const skill of skills) {
    m[`/${skill.name}`] = skill.description;
  }
  return m;
}

export function buildSlashCommandWithArgsSet(
  skills: ReadonlyArray<{ name: string; description: string }>,
  showEffort: boolean,
  showModel: boolean,
): Set<string> {
  const s = new Set<string>();
  if (showEffort) s.add("/effort");
  if (showModel) s.add("/model");
  for (const skill of skills) s.add(`/${skill.name}`);
  return s;
}

export function subAgentComposerLabel(
  session: Pick<Session, "parentSessionId" | "title" | "subAgentName" | "agentName"> | null,
): string | null {
  if (!session || session.parentSessionId == null) return null;
  let title = session.title ?? null;
  if (title?.startsWith("ui:")) title = title.slice(3);
  if (title?.includes(":")) {
    const suffix = title.split(":").slice(1).join(":");
    if (suffix) return suffix;
  }
  return title ?? session.subAgentName ?? session.agentName ?? "sub-agent";
}

export function computeIsWorking(sessionStatus: SessionStatus): boolean {
  return sessionStatus === "running" || sessionStatus === "waiting";
}

export function computeShowsWorking(
  sessionStatus: SessionStatus,
  options: { hasPendingElicitation: boolean; runnerOnline: boolean | undefined },
): boolean {
  if (options.runnerOnline === false) return false;
  if (options.hasPendingElicitation) return false;
  return computeIsWorking(sessionStatus);
}

export function shouldSendInitialPrompt(params: {
  initialPrompt: string | null;
  promptConversationId: string | null;
  sentForConversationId: string | null;
  conversationId: string | null | undefined;
  loadingConversation: boolean;
  agentId: string | null;
}): boolean {
  if (!params.initialPrompt) return false;
  if (params.promptConversationId !== params.conversationId) return false;
  if (params.sentForConversationId === params.conversationId) return false;
  if (!params.conversationId || params.loadingConversation || !params.agentId) {
    return false;
  }
  return true;
}

export function dispatchInitialPrompt(
  prompt: PendingInitialPrompt,
  agentId: string,
  send: (text: string, agentId: string, files: File[]) => Promise<void>,
  sendSlashCommand: (name: string, args: string, agentId: string) => Promise<void>,
): void {
  if (prompt.skill) {
    void sendSlashCommand(prompt.skill.name, prompt.skill.args, agentId);
  } else {
    void send(prompt.text, agentId, prompt.files ?? []);
  }
}

export function isUnboundCodingFork(params: {
  forkSourceId: string | null;
  workspace: string | null | undefined;
}): boolean {
  return params.forkSourceId !== null && !params.workspace;
}

const EFFORT_LEVELS = ["low", "medium", "high"] as const;
const CLAUDE_NATIVE_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"] as const;

type LabelSource = { labels?: Record<string, string | null> | null } | null | undefined;

export function readOnlyReasonForSessionLabels(
  activeSession: LabelSource,
  activeConv: LabelSource,
): string | null {
  const closed =
    activeSession?.labels?.["omnigent.closed"] ?? activeConv?.labels?.["omnigent.closed"];
  if (closed === "true") return "This sub-agent session is closed";
  const wrapper =
    activeSession?.labels?.["omnigent.wrapper"] ?? activeConv?.labels?.["omnigent.wrapper"];
  if (wrapper === "claude-code-native-ui-subagent") {
    return "Claude Code sub-agents are read-only";
  }
  return null;
}

export function effortLevelsForConv(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): readonly string[] {
  if (conv?.labels?.["omnigent.wrapper"] === "claude-code-native-ui") {
    return CLAUDE_NATIVE_EFFORT_LEVELS;
  }
  return EFFORT_LEVELS;
}

export function shouldShowModelPicker(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  return conv?.labels?.["omnigent.wrapper"] === "claude-code-native-ui";
}

export function shouldShowEffortPicker(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  return supportsEffortControl(conv);
}

export function isModelImplicitlySelected(modelId: string, llmModel: string | null): boolean {
  if (!llmModel) return false;
  return llmModel === modelId || llmModel.endsWith(`/${modelId}`) || llmModel.includes(modelId);
}

/** Title-case an effort level for the trigger pill (``"high"`` → ``"High"``). */
export function formatEffortLabel(effort: string): string {
  return effort.charAt(0).toUpperCase() + effort.slice(1);
}

export function shouldShowTerminalSurface(
  conversationId: string | null,
  terminalFirst:
    | {
        isTerminalFirst: boolean;
        view: "chat" | "terminal";
      }
    | null
    | undefined,
  _runnerOnline: boolean | undefined,
): boolean {
  return (
    !!conversationId && terminalFirst?.isTerminalFirst === true && terminalFirst.view === "terminal"
  );
}