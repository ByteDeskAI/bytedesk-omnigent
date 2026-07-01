import {
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { BUILTIN_SLASH_COMMANDS, isSlashCommandText } from "@/components/SlashCommandMenu";
import { usePromptHistory } from "@/hooks/usePromptHistory";
import { useAutoGrowTextarea } from "@/hooks/useAutoGrowTextarea";
import { consumeShareDraft } from "@/pages/SharePage";
import { useChatStore } from "@/store/chatStore";
import {
  buildSlashCommandMap,
  buildSlashCommandWithArgsSet,
  saveDraftsToStorage,
  sessionDrafts,
} from "../chat-utils";
import type { ComposerProps } from "./composer-types";
import { executeSlashCommand } from "./executeSlashCommand";

export function useComposer(props: ComposerProps) {
  const {
    status,
    isWorking,
    disabled,
    onSend,
    onSendSlashCommand,
    onStop,
    permissionLevel,
    readOnlyReason,
    replyQuotes,
    onClearAllQuotes,
    effortLevels,
    showEffort,
    showModels,
    isNativeWrapper = false,
    reconnectHint = false,
    unreachable = false,
  } = props;

  const [value, setValue] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [menuIndex, setMenuIndex] = useState(-1);
  const [pickerOpenNonce, setPickerOpenNonce] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const isStreaming = status === "streaming";
  const isReadOnly = permissionLevel === 1 || readOnlyReason !== null;
  const hasPendingElicitation = useChatStore((s) =>
    s.blocks.some(
      (b) =>
        b.type === "elicitation" &&
        b.status === "pending" &&
        (b.targetSessionId == null || b.targetSessionId === s.conversationId),
    ),
  );
  const costControlModeOverride = useChatStore((s) => s.costControlModeOverride);
  const conversationId = useChatStore((s) => s.conversationId);
  const valueRef = useRef(value);
  valueRef.current = value;
  const filesRef = useRef(files);
  filesRef.current = files;
  const dirtyRef = useRef(false);

  useEffect(() => {
    const restored = conversationId ? sessionDrafts.get(conversationId) : undefined;
    const shareDraft = conversationId ? null : consumeShareDraft();
    setValue(shareDraft ?? restored?.text ?? "");
    setFiles(restored?.files ?? []);
    dirtyRef.current = false;
    textareaRef.current?.focus();

    return () => {
      if (!conversationId || !dirtyRef.current) return;
      const text = valueRef.current;
      const draftFiles = filesRef.current;
      if (text || draftFiles.length > 0) {
        sessionDrafts.set(conversationId, { text, files: draftFiles });
      } else {
        sessionDrafts.delete(conversationId);
      }
      saveDraftsToStorage(sessionDrafts);
    };
  }, [conversationId]);

  const prevQuoteCountRef = useRef(replyQuotes.length);
  useEffect(() => {
    if (replyQuotes.length > prevQuoteCountRef.current) {
      textareaRef.current?.focus();
    }
    prevQuoteCountRef.current = replyQuotes.length;
  }, [replyQuotes.length]);

  const skills = useChatStore((s) => s.skills);
  const showModel = !isNativeWrapper || showModels;
  const slashCommands = useMemo(
    () => buildSlashCommandMap(skills, showEffort, showModel),
    [skills, showEffort, showModel],
  );
  const slashCommandsWithArgs = useMemo(
    () => buildSlashCommandWithArgsSet(skills, showEffort, showModel),
    [skills, showEffort, showModel],
  );

  const trimmedValue = value.trimStart();
  const menuOpen =
    trimmedValue.startsWith("/") &&
    !trimmedValue.slice(1).includes("/") &&
    !trimmedValue.includes(" ") &&
    files.length === 0;
  const menuQuery = menuOpen ? trimmedValue.slice(1) : "";
  const composerIsCommand = files.length === 0 && isSlashCommandText(value);
  const hasDraft = value.trim().length > 0 || files.length > 0;
  const showInterruptButton = isWorking && !hasDraft;
  const menuMatches = menuOpen
    ? Object.keys(slashCommands).filter((name) => name.slice(1).startsWith(menuQuery.toLowerCase()))
    : [];

  const prevMenuMatchesRef = useRef<string[]>([]);
  if (
    menuMatches.length !== prevMenuMatchesRef.current.length ||
    menuMatches.some((m, i) => m !== prevMenuMatchesRef.current[i])
  ) {
    prevMenuMatchesRef.current = menuMatches;
    setMenuIndex(menuMatches.length > 0 ? 0 : -1);
  }

  const slashCtx = useMemo(
    () => ({
      showEffort,
      showModel,
      effortLevels,
      slashCommands,
      dirtyRef,
      setValue,
      setCommandError,
    }),
    [showEffort, showModel, effortLevels, slashCommands],
  );

  const applyMenuSelection = (cmd: string) => {
    setMenuIndex(-1);
    if (slashCommandsWithArgs.has(cmd)) {
      setValue(cmd + " ");
      dirtyRef.current = true;
      textareaRef.current?.focus();
    } else {
      setValue("");
      setCommandError(null);
      executeSlashCommand(cmd, "", slashCtx);
    }
  };

  const [isMobile, setIsMobile] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(max-width: 767px)").matches,
  );
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 767px)");
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  useAutoGrowTextarea(textareaRef, value);

  const { appendEntry, recallPrevious, recallNext, resetCursor } = usePromptHistory(conversationId);
  const recallingRef = useRef(false);
  const [isDragActive, setIsDragActive] = useState(false);

  const addFiles = (incoming: File[]) => {
    setFiles((prev) => [...prev, ...incoming]);
    dirtyRef.current = true;
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(false);
    const dropped = Array.from(e.dataTransfer.files);
    if (dropped.length > 0) addFiles(dropped);
  };

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(true);
  };

  const handleDragEnter = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(true);
  };

  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setIsDragActive(false);
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
    dirtyRef.current = true;
  };

  const submit = () => {
    const trimmed = value.trim();
    if ((!trimmed && files.length === 0) || disabled || hasPendingElicitation) return;

    if (isSlashCommandText(trimmed) && files.length === 0) {
      const parts = trimmed.split(/\s+/);
      const cmd = parts[0].toLowerCase();
      const arg = parts[1] ?? "";
      if (cmd === "/model" && !arg && showModels) {
        dirtyRef.current = true;
        setValue("");
        setCommandError(null);
        setPickerOpenNonce((n) => n + 1);
        return;
      }
      if (cmd in BUILTIN_SLASH_COMMANDS && cmd in slashCommands) {
        executeSlashCommand(cmd, arg, slashCtx);
        return;
      }
      if (onSendSlashCommand && parts[0] in slashCommands) {
        const skillArgs = trimmed.slice(parts[0].length).trim();
        appendEntry(trimmed);
        onSendSlashCommand(parts[0].slice(1), skillArgs);
        dirtyRef.current = true;
        setValue("");
        setCommandError(null);
        onClearAllQuotes();
        return;
      }
    }

    setCommandError(null);
    const quotePreamble =
      replyQuotes.length > 0
        ? replyQuotes
            .map((q) =>
              q
                .split("\n")
                .map((line) => `> ${line}`)
                .join("\n"),
            )
            .join("\n\n") + "\n\n"
        : "";
    const messageText = quotePreamble + trimmed;
    if (trimmed) appendEntry(trimmed);
    onSend(messageText, files.length > 0 ? files : undefined);
    dirtyRef.current = true;
    setValue("");
    setFiles([]);
    onClearAllQuotes();
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (showInterruptButton) {
      onStop();
      return;
    }
    submit();
  };

  const applyRecall = (ta: HTMLTextAreaElement, recalled: string) => {
    recallingRef.current = true;
    setValue(recalled);
    dirtyRef.current = true;
    queueMicrotask(() => {
      ta.setSelectionRange(recalled.length, recalled.length);
    });
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (menuOpen && menuMatches.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMenuIndex((i) => (i + 1) % menuMatches.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMenuIndex((i) => (i <= 0 ? menuMatches.length - 1 : i - 1));
        return;
      }
      if ((e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !isMobile)) && menuIndex >= 0) {
        e.preventDefault();
        applyMenuSelection(menuMatches[menuIndex]!);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setValue("");
        setMenuIndex(-1);
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey && !isMobile && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
      return;
    }
    if (e.key === "Escape" && isStreaming) {
      e.preventDefault();
      onStop();
      return;
    }
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      const ta = e.currentTarget;
      if (e.key === "ArrowUp" && ta.selectionStart === 0) {
        const recalled = recallPrevious(value);
        if (recalled !== null) {
          e.preventDefault();
          applyRecall(ta, recalled);
        }
      } else if (e.key === "ArrowDown" && ta.selectionEnd === ta.value.length) {
        const recalled = recallNext();
        if (recalled !== null) {
          e.preventDefault();
          applyRecall(ta, recalled);
        }
      }
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const pastedFiles: File[] = [];
    for (const item of items) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) pastedFiles.push(file);
      }
    }
    if (pastedFiles.length > 0) {
      e.preventDefault();
      addFiles(pastedFiles);
    }
  };

  const placeholder =
    readOnlyReason !== null
      ? readOnlyReason
      : isReadOnly
        ? "You have read-only access to this session"
        : unreachable
          ? "Session offline — reconnect below to continue"
          : hasPendingElicitation
            ? "Respond to the pending request above to continue"
            : disabled
              ? "Waiting for agents…"
              : isStreaming
                ? "Send a follow-up (queued) — Esc to stop"
                : reconnectHint
                  ? "Send a message to reconnect this session"
                  : "Ask the agent anything…";

  return {
    value,
    setValue,
    files,
    commandError,
    setCommandError,
    menuIndex,
    menuOpen,
    menuQuery,
    menuMatches,
    composerIsCommand,
    hasDraft,
    showInterruptButton,
    isReadOnly,
    hasPendingElicitation,
    costControlModeOverride,
    isDragActive,
    fileInputRef,
    textareaRef,
    backdropRef,
    slashCommands,
    applyMenuSelection,
    handleSubmit,
    handleKeyDown,
    handlePaste,
    handleDrop,
    handleDragOver,
    handleDragEnter,
    handleDragLeave,
    addFiles,
    removeFile,
    resetCursor,
    recallingRef,
    dirtyRef,
    placeholder,
    pickerOpenNonce,
  };
}