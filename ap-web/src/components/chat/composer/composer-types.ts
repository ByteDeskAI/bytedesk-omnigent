import type { CostRoutingVerdict } from "@/components/CostRoutingControl";
import type { Agent } from "@/hooks/useAgents";

export interface ComposerProps {
  status: "idle" | "streaming";
  /** Local stream OR cross-client `session.status: running`. */
  isWorking: boolean;
  disabled: boolean;
  onSend: (text: string, files?: File[]) => void;
  /**
   * Send a recognised skill as a `slash_command` event (the REPL's wire
   * shape) instead of plaintext. When present and the typed command names
   * a known session skill, `submit()` routes through this; otherwise the
   * command falls through to `onSend` as plaintext. Undefined for
   * native-terminal sessions, which always send `/skill` as plaintext so
   * the vendor TUI loads the skill itself.
   */
  onSendSlashCommand?: (name: string, args: string) => void;
  onStop: () => void;
  agents: Agent[] | undefined;
  agentsLoading: boolean;
  selectedAgentId: string | null;
  onSelectAgent: (id: string) => void;
  permissionLevel: number | null;
  /**
   * When non-null, the composer is forced read-only and the string is
   * shown as the textarea placeholder. Distinct from
   * ``permissionLevel === 1`` (which means "user has read-only
   * grant") — this captures the "this session structurally can't be
   * interacted with" case: e.g. a claude-native sub-agent whose
   * transcript is mirrored from disk and has no input surface. ``null``
   * leaves the existing ``permissionLevel`` gate alone.
   */
  readOnlyReason: string | null;
  /** Quoted texts to prepend to the next message (one per "Reply ↵" click). */
  replyQuotes: string[];
  /** Removes the quote at the given index without submitting. */
  onRemoveQuote: (index: number) => void;
  /** Clears all quotes (called after submit). */
  onClearAllQuotes: () => void;
  /** Reasoning-effort options to render in `/effort` and the picker dropdown. */
  effortLevels: readonly string[];
  /** Show `/effort` and the Effort picker section. */
  showEffort: boolean;
  /** Whether the picker dropdown should include a Models section. */
  showModels: boolean;
  /**
   * Terminal-first session (Chat/Terminal pill present). Presentation
   * only: tightens the composer's bottom padding to `pb-1.5` so it sits
   * closer to the pill beneath it; non-terminal-first chats use the
   * roomier `pb-3`.
   */
  isTerminalFirst?: boolean;
  /**
   * Native-CLI wrapper session (claude-native / codex-native). Drops the
   * `/model` slash command unless the session also has the model picker
   * (`showModels`, claude-native — the runner propagates the override
   * live). Codex-native pins its model at launch, so the command would
   * be a misleading no-op there. Terminal-first SDK sessions (embedded
   * Omnigent REPL terminal) keep it.
   */
  isNativeWrapper?: boolean;
  /**
   * The session's runner is asleep but its host is online (`runner_asleep`):
   * the composer stays enabled and the placeholder nudges the user to send a
   * message, which relaunches the runner on the live host. Ignored while a
   * turn is streaming (the follow-up placeholder wins).
   */
  reconnectHint?: boolean;
  /**
   * The session is unreachable (`host_offline` / `local_stranded`): a message
   * can't wake it. The composer is blocked (disabled) and the reconnect
   * banner below is the only affordance.
   */
  unreachable?: boolean;
  /** Latest parsed advisor verdict for the cost-routing pill; `null`/omitted when none. */
  costRoutingVerdict?: CostRoutingVerdict | null;
  /** Session passes `isCostRoutingSession` (polly orchestrator, not a child); see that predicate. */
  costRoutingEligible?: boolean;
  /**
   * Sub-agent instance label when the active session is a child, e.g.
   * ``"check-account-eligibility"``; ``null``/omitted for top-level
   * sessions. When set, the composer peeks a "Chatting with sub-agent …"
   * tray above the card. See ``subAgentComposerLabel``.
   */
  subAgentLabel?: string | null;
}