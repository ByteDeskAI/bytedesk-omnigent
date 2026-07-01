import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import type { BundledLanguage, ThemedToken } from "shiki";
import { highlightCode } from "@/components/ai-elements/code-block";
import { type Comment } from "@/hooks/useComments";
import { useFileContent } from "@/hooks/useFileContent";
import { useCanEdit } from "@/hooks/usePermissions";
import {
  type ActiveSelection,
  type SaveStatus,
  detectLang,
  getSelectionOffsets,
  indexToLine,
} from "../../codeViewerHelpers";

const GUTTER_WIDTH = 48;

interface UseCodeViewerStateArgs {
  conversationId: string;
  path: string;
  fileQuery: ReturnType<typeof useFileContent>;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (
    sel: { start_index: number; end_index: number; anchor_content: string } | null,
  ) => void;
  panelOpen: boolean;
  searchOpen: boolean;
  setSearchOpen: (open: boolean) => void;
  searchInputRef: RefObject<HTMLInputElement | null>;
  viewMode: "editor" | "preview" | "source" | "diff";
}

export function useCodeViewerState({
  conversationId,
  path,
  fileQuery,
  comments,
  activeSelection,
  onSetActiveSelection,
  panelOpen,
  searchOpen,
  setSearchOpen,
  searchInputRef,
  viewMode,
}: UseCodeViewerStateArgs) {
  const canEdit = useCanEdit(conversationId);
  const [tokenLines, setTokenLines] = useState<ThemedToken[][] | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [currentMatchIdx, setCurrentMatchIdx] = useState(0);
  const matchLineRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const [selectionAnchor, setSelectionAnchor] = useState<{
    x: number;
    y: number;
    start_index: number;
    end_index: number;
    anchor_content: string;
  } | null>(null);

  const codeContainerRef = useRef<HTMLDivElement>(null);
  const selectAllPendingRef = useRef(false);
  const commentsRef = useRef(comments);
  useEffect(() => {
    commentsRef.current = comments;
  }, [comments]);
  const onSetActiveSelectionRef = useRef(onSetActiveSelection);
  useEffect(() => {
    onSetActiveSelectionRef.current = onSetActiveSelection;
  }, [onSetActiveSelection]);
  const canEditRef = useRef(canEdit);
  useEffect(() => {
    canEditRef.current = canEdit;
  }, [canEdit]);

  const content = fileQuery.data?.content ?? "";
  const truncated = fileQuery.data?.truncated ?? false;
  const lang = detectLang(path);
  const showMonaco = lang !== "markdown" && viewMode !== "preview";
  const rawLines = useMemo(() => (showMonaco ? [] : content.split("\n")), [content, showMonaco]);

  useEffect(() => {
    if (showMonaco) return;
    if (viewMode === "editor" && lang === "markdown") return;
    let cancelled = false;
    setTokenLines(null);
    if (!content) return;
    const cached = highlightCode(content, lang as BundledLanguage, (result) => {
      if (!cancelled) setTokenLines(result.tokens);
    });
    if (cached) setTokenLines(cached.tokens);
    return () => {
      cancelled = true;
    };
  }, [content, lang, viewMode, showMonaco]);

  useEffect(() => {
    if (activeSelection == null) return;
    const lineNum = indexToLine(activeSelection.start_index, rawLines);
    matchLineRefs.current.get(lineNum - 1)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [activeSelection]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setCurrentMatchIdx(0);
  }, [searchQuery]);
  useEffect(() => {
    if (!searchOpen) setSearchQuery("");
  }, [searchOpen]);

  useEffect(() => {
    if (!searchQuery.trim()) return;
    const matches = rawLines
      .map((line, i) => (line.toLowerCase().includes(searchQuery.toLowerCase()) ? i : -1))
      .filter((i) => i !== -1);
    if (matches.length === 0) return;
    const idx = matches[currentMatchIdx % matches.length];
    matchLineRefs.current.get(idx)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [searchQuery, currentMatchIdx]); // eslint-disable-line react-hooks/exhaustive-deps

  const isMarkdownEditor = viewMode === "editor" && lang === "markdown";
  useEffect(() => {
    if (!panelOpen || isMarkdownEditor || showMonaco) return;
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "f") {
        e.preventDefault();
        setSearchOpen(true);
        setTimeout(() => searchInputRef.current?.focus(), 0);
      } else if ((e.metaKey || e.ctrlKey) && e.key === "a") {
        const container = codeContainerRef.current;
        if (!container) return;
        const active = document.activeElement;
        if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) return;
        e.preventDefault();
        const selection = window.getSelection();
        if (!selection) return;
        const range = document.createRange();
        range.selectNodeContents(container);
        selection.removeAllRanges();
        selection.addRange(range);
        selectAllPendingRef.current = true;
      } else if (e.key === "Escape") {
        setSearchOpen(false);
        setSearchQuery("");
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [panelOpen, isMarkdownEditor, showMonaco, setSearchOpen, searchInputRef]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSearchHandled = useCallback(() => setSearchOpen(false), [setSearchOpen]);

  useEffect(() => {
    const container = codeContainerRef.current;
    if (!container) return;
    const handleMouseUp = (e: MouseEvent) => {
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return;
      const range = sel.getRangeAt(0);

      if (sel.isCollapsed) {
        if ((e.target as Element).closest("[data-gutter-comment]")) return;
        if (container.contains(range.commonAncestorContainer)) {
          const offsets = getSelectionOffsets(range, container, rawLines);
          if (offsets) {
            const clicked = commentsRef.current.find(
              (c) => c.start_index <= offsets.start_index && offsets.start_index < c.end_index,
            );
            if (clicked) {
              onSetActiveSelectionRef.current({
                start_index: clicked.start_index,
                end_index: clicked.end_index,
                anchor_content: clicked.anchor_content ?? "",
              });
              return;
            }
          }
        }
        onSetActiveSelectionRef.current(null);
        return;
      }

      if (!canEditRef.current) return;
      if (!container.contains(range.commonAncestorContainer)) return;
      const anchor_content = sel.toString();
      if (!anchor_content.trim()) return;
      const offsets = getSelectionOffsets(range, container, rawLines);
      if (!offsets) return;
      const firstRect = range.getClientRects()[0] ?? range.getBoundingClientRect();
      const containerLeft = container.getBoundingClientRect().left;
      setSelectionAnchor({
        x: Math.max(firstRect.left, containerLeft + GUTTER_WIDTH),
        y: firstRect.top - 6,
        ...offsets,
        anchor_content,
      });
    };
    container.addEventListener("mouseup", handleMouseUp);
    return () => container.removeEventListener("mouseup", handleMouseUp);
  }, [rawLines]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const handleMouseDown = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest("[data-add-comment-btn]")) {
        setSelectionAnchor(null);
      }
    };
    document.addEventListener("mousedown", handleMouseDown);
    return () => document.removeEventListener("mousedown", handleMouseDown);
  }, []);

  useEffect(() => {
    const handleCopy = (e: ClipboardEvent) => {
      if (!selectAllPendingRef.current) return;
      selectAllPendingRef.current = false;
      e.preventDefault();
      e.clipboardData?.setData("text/plain", content);
    };
    const clearFlag = () => {
      selectAllPendingRef.current = false;
    };
    document.addEventListener("copy", handleCopy);
    document.addEventListener("mousedown", clearFlag);
    return () => {
      document.removeEventListener("copy", handleCopy);
      document.removeEventListener("mousedown", clearFlag);
    };
  }, [content]);

  const matches = searchQuery.trim()
    ? rawLines
        .map((line, i) => (line.toLowerCase().includes(searchQuery.toLowerCase()) ? i : -1))
        .filter((i) => i !== -1)
    : [];
  const safeMatchIdx = matches.length > 0 ? currentMatchIdx % matches.length : 0;
  const matchLabel = searchQuery.trim()
    ? matches.length > 0
      ? `${safeMatchIdx + 1} / ${matches.length}`
      : "No results"
    : "";

  return {
    content,
    truncated,
    lang,
    showMonaco,
    rawLines,
    tokenLines,
    codeContainerRef,
    selectionAnchor,
    setSelectionAnchor,
    searchQuery,
    setSearchQuery,
    matches,
    safeMatchIdx,
    matchLabel,
    matchLineRefs,
    setCurrentMatchIdx,
    handleSearchHandled,
  };
}

export type { SaveStatus };