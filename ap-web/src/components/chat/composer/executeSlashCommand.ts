import { useChatStore } from "@/store/chatStore";

export interface SlashCommandContext {
  showEffort: boolean;
  showModel: boolean;
  effortLevels: readonly string[];
  slashCommands: Record<string, string>;
  dirtyRef: React.MutableRefObject<boolean>;
  setValue: (value: string) => void;
  setCommandError: (error: string | null) => void;
}

/**
 * Execute a slash command by name + optional argument string.
 * Clears the input and error state on success (or sets an error on
 * bad usage). Returns ``true`` when the command was recognised.
 */
export function executeSlashCommand(
  cmd: string,
  arg: string,
  ctx: SlashCommandContext,
): boolean {
  const { showEffort, showModel, effortLevels, slashCommands, dirtyRef, setValue, setCommandError } =
    ctx;

  switch (cmd) {
    case "/compact":
      dirtyRef.current = true;
      setValue("");
      setCommandError(null);
      void useChatStore
        .getState()
        .compact()
        .catch((err: unknown) => {
          setCommandError(err instanceof Error ? err.message : "Compact failed");
        });
      return true;
    case "/effort": {
      if (!showEffort) return false;
      const valid = [...effortLevels, "default"];
      if (!arg || !valid.includes(arg.toLowerCase())) {
        setCommandError(`Usage: /effort ${valid.join(" | ")}`);
        return true;
      }
      const level = arg.toLowerCase() === "default" ? null : arg.toLowerCase();
      dirtyRef.current = true;
      setValue("");
      setCommandError(null);
      void useChatStore
        .getState()
        .setEffort(level)
        .catch((err: unknown) => {
          setCommandError(err instanceof Error ? err.message : "Failed to set effort");
        });
      return true;
    }
    case "/model": {
      if (!showModel) return false;
      const target = arg.trim();
      if (!target) {
        const { sessionModelOverride, llmModel } = useChatStore.getState();
        const current = sessionModelOverride
          ? `${sessionModelOverride} (override)`
          : (llmModel ?? "agent default");
        setCommandError(`Model: ${current}\nUsage: /model <name> · /model default to reset`);
        return true;
      }
      const clear = ["default", "off", "reset"].includes(target.toLowerCase());
      dirtyRef.current = true;
      setValue("");
      setCommandError(null);
      void useChatStore
        .getState()
        .setModel(clear ? null : target)
        .catch((err: unknown) => {
          setCommandError(err instanceof Error ? err.message : "Failed to set model");
        });
      return true;
    }
    case "/context": {
      const state = useChatStore.getState();
      const { contextWindow, llmModel, sessionModelOverride, tokensUsed, blocks } = state;
      const lines: string[] = [];
      if (sessionModelOverride) lines.push(`Model: ${sessionModelOverride} (override)`);
      else if (llmModel) lines.push(`Model: ${llmModel}`);
      if (tokensUsed != null && contextWindow != null && contextWindow > 0) {
        const pct = Math.min(tokensUsed / contextWindow, 1);
        const filled = Math.round(pct * 20);
        const bar = "█".repeat(filled) + "░".repeat(20 - filled);
        const pctStr = (pct * 100).toFixed(1);
        lines.push(
          `${tokensUsed.toLocaleString()} / ${contextWindow.toLocaleString()} tokens (${pctStr}%)`,
        );
        lines.push(bar);
      } else if (tokensUsed != null) {
        lines.push(`${tokensUsed.toLocaleString()} tokens`);
        lines.push("(Context window size unknown)");
      } else {
        lines.push("No usage data yet — send a message first.");
      }
      lines.push(`Items in context: ${blocks.length}`);
      setCommandError(lines.join("\n"));
      return true;
    }
    case "/help": {
      const lines = Object.entries(slashCommands).map(([name, desc]) => `${name} — ${desc}`);
      setCommandError(lines.join("\n"));
      return true;
    }
    default:
      setCommandError(
        `Unknown command: ${cmd}. Available: ${Object.keys(slashCommands).join(", ")}`,
      );
      return false;
  }
}