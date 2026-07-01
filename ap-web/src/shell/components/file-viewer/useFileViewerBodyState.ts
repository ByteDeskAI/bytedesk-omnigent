import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "@/lib/routing";
import { fileContentToBlob, triggerBrowserDownload, useFileContent } from "@/hooks/useFileContent";
import { useFileDiff } from "@/hooks/useFileDiff";
import { useComments } from "@/hooks/useComments";
import { markCommentsSeen } from "@/hooks/useSeenComments";
import { useResizablePanel } from "@/hooks/useResizablePanel";
import { useWorkspaceChangedFiles } from "@/hooks/useWorkspaceChangedFiles";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";
import { compareChangedFiles } from "../../FlatFileList";
import { detectLang, MONACO_SPLIT_BREAKPOINT, type SaveStatus } from "../../codeViewerHelpers";
import { type ActiveSelection } from "../../CommentsPanel";
import { buildFileViewerToolbarActions } from "./buildFileViewerToolbarActions";
import { classifyAndRemapComments } from "./fileViewerUtils";

interface UseFileViewerBodyStateArgs {
  open: boolean;
  conversationId: string;
  path: string;
  onNavigateTo?: (path: string) => void;
  permissionLevel?: number | null;
  frameless?: boolean;
  onCommentsOpenChange?: (open: boolean) => void;
  sort?: import("../../FlatFileList").ChangedSort;
}

export function useFileViewerBodyState({
  open,
  conversationId,
  path,
  onNavigateTo,
  permissionLevel,
  frameless,
  onCommentsOpenChange,
  sort = "recent",
}: UseFileViewerBodyStateArgs) {
  const canEdit = permissionLevel == null || permissionLevel >= 2;
  const [searchParams, setSearchParams] = useSearchParams();
  const initialDiffRef = useRef(searchParams.get("diff") === "1");
  const initialCommentIdRef = useRef(searchParams.get("comment"));
  const [commentsOpen, setCommentsOpen] = useState(false);
  const COMMENTS_PANEL_WIDTH_PX = 240;
  const minWidthPx = commentsOpen ? 480 + COMMENTS_PANEL_WIDTH_PX : undefined;
  const { panelWidth, handleProps, isDesktop } = useResizablePanel(
    frameless ? false : open,
    50,
    frameless ? undefined : minWidthPx,
  );
  const fileQuery = useFileContent(conversationId, path);
  const diffQuery = useFileDiff(conversationId, path);
  const changedFiles = useWorkspaceChangedFiles(conversationId);

  const navigableFiles = useMemo(
    () => [...(changedFiles.data?.data ?? [])].sort(compareChangedFiles(sort)).map((f) => f.path),
    [changedFiles.data?.data, sort],
  );
  const currentNavIdx = navigableFiles.indexOf(path);
  const prevPath = currentNavIdx > 0 ? navigableFiles[currentNavIdx - 1] : null;
  const nextPath =
    currentNavIdx >= 0 && currentNavIdx < navigableFiles.length - 1
      ? navigableFiles[currentNavIdx + 1]
      : null;
  const commentsQuery = useComments(conversationId, path);
  const commentsInitializedRef = useRef(false);
  const linkedCommentAppliedRef = useRef(false);
  const viewModeInitializedRef = useRef(false);
  const prevOpenRef = useRef(open);
  const [activeSelection, setActiveSelection] = useState<ActiveSelection | null>(null);
  const contentAreaRef = useRef<HTMLDivElement | null>(null);
  const [contentWidth, setContentWidth] = useState<number | null>(null);
  const pendingBodyRef = useRef("");
  const [isEditorDirty, setIsEditorDirty] = useState(false);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");
  const [linkCopied, setLinkCopied] = useState(false);
  const linkCopiedTimerRef = useRef<number>(0);
  const [pendingAction, setPendingAction] = useState<(() => void) | null>(null);

  useEffect(() => {
    setActiveSelection(null);
    setIsEditorDirty(false);
    setSaveStatus("idle");
  }, [path]);

  useEffect(() => {
    if (open && !prevOpenRef.current) {
      commentsInitializedRef.current = false;
    }
    prevOpenRef.current = open;
  }, [open]);

  useEffect(() => {
    if (!open || commentsInitializedRef.current) return;
    if (commentsQuery.data === undefined) return;
    commentsInitializedRef.current = true;
  }, [open, commentsQuery.data]);

  useEffect(() => {
    if (!open || !commentsOpen || commentsQuery.data === undefined) return;
    markCommentsSeen(commentsQuery.data.map((c) => c.id));
  }, [open, commentsOpen, commentsQuery.data]);

  useEffect(() => {
    onCommentsOpenChange?.(commentsOpen);
  }, [commentsOpen, onCommentsOpenChange]);

  useEffect(() => {
    if (!isEditorDirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isEditorDirty]);

  const guardDirty = useCallback(
    (action: () => void) => {
      if (isEditorDirty) {
        setPendingAction(() => action);
        return;
      }
      action();
    },
    [isEditorDirty],
  );

  const handleSetActiveSelection = (sel: ActiveSelection | null) => {
    setActiveSelection(sel);
    if (sel !== null) {
      commentsInitializedRef.current = true;
      setCommentsOpen(true);
    }
  };

  useEffect(
    () => () => {
      window.clearTimeout(linkCopiedTimerRef.current);
    },
    [],
  );

  const downloadFile = useCallback(() => {
    const data = fileQuery.data;
    if (!data) return;
    triggerBrowserDownload(fileContentToBlob(data), path.split("/").pop() ?? path);
  }, [fileQuery.data, path]);

  const copyFileLink = useCallback(() => {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return;
    const url = new URL(window.location.href);
    if (!activeSelection) url.searchParams.delete("comment");
    navigator.clipboard.writeText(url.toString()).then(
      () => {
        setLinkCopied(true);
        window.clearTimeout(linkCopiedTimerRef.current);
        linkCopiedTimerRef.current = window.setTimeout(() => setLinkCopied(false), 2000);
      },
      (err) => console.warn("Failed to copy file link", err),
    );
  }, [activeSelection]);

  const copyCommentLink = useCallback((commentId: string) => {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return;
    const url = new URL(window.location.href);
    url.searchParams.set("comment", commentId);
    navigator.clipboard
      .writeText(url.toString())
      .then(undefined, (err) => console.warn("Failed to copy comment link", err));
  }, []);

  const allComments = useMemo(() => commentsQuery.data ?? [], [commentsQuery.data]);
  const fileContent = useMemo(() => fileQuery.data?.content ?? "", [fileQuery.data]);
  const { open: openComments, addressed: addressedComments } = useMemo(
    () => classifyAndRemapComments(allComments, fileContent),
    [allComments, fileContent], // eslint-disable-line react-hooks/exhaustive-deps
  );

  useEffect(() => {
    if (linkedCommentAppliedRef.current) return;
    const commentId = initialCommentIdRef.current;
    if (!commentId || fileQuery.data === undefined) return;
    const comment = openComments.find((c) => c.id === commentId);
    if (!comment) return;
    linkedCommentAppliedRef.current = true;
    commentsInitializedRef.current = true;
    setCommentsOpen(true);
    setActiveSelection({
      start_index: comment.start_index,
      end_index: comment.end_index,
      anchor_content: comment.anchor_content ?? "",
    });
  }, [openComments]); // eslint-disable-line react-hooks/exhaustive-deps

  const [searchOpen, setSearchOpen] = useState(false);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const openSearch = useCallback(() => {
    setSearchOpen((prev) => {
      if (prev) return false;
      setTimeout(() => searchInputRef.current?.focus(), 0);
      return true;
    });
  }, [searchInputRef]);

  useEffect(() => {
    if (!open || !onNavigateTo || currentNavIdx === -1) return;
    const handler = (e: KeyboardEvent) => {
      if (!e.altKey) return;
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const target = e.target;
      if (
        target instanceof HTMLElement &&
        target.closest('textarea, input, [contenteditable="true"]')
      ) {
        return;
      }
      if (e.key === "ArrowLeft" && prevPath) {
        e.preventDefault();
        guardDirty(() => onNavigateTo(prevPath));
      } else if (e.key === "ArrowRight" && nextPath) {
        e.preventDefault();
        guardDirty(() => onNavigateTo(nextPath));
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onNavigateTo, currentNavIdx, prevPath, nextPath, guardDirty]);

  const lang = detectLang(path);
  const isPreviewable = lang === "markdown" || lang === "html";
  const isDiffAvailable = changedFiles.data?.data.some((f) => f.path === path) ?? false;
  const isDeletedFile =
    changedFiles.data?.data.some((f) => f.path === path && f.status === "deleted") ?? false;

  const persistedPrefsRef = useRef(readFileViewPreferences());
  const [diffActive, setDiffActive] = useState(
    () => initialDiffRef.current || persistedPrefsRef.current.diffActive,
  );
  const [diffLayout, setDiffLayout] = useState<"unified" | "split">(
    () => persistedPrefsRef.current.diffLayout,
  );
  const [previewableViewMode, setPreviewableViewMode] = useState<"editor" | "preview" | "source">(
    () => persistedPrefsRef.current.previewableViewMode,
  );

  useEffect(() => {
    writeFileViewPreferences({ diffActive, diffLayout, previewableViewMode });
  }, [diffActive, diffLayout, previewableViewMode]);

  const fileViewMode: "editor" | "preview" | "source" = isPreviewable
    ? lang !== "markdown" && previewableViewMode === "editor"
      ? "preview"
      : lang === "markdown" && previewableViewMode === "preview"
        ? "source"
        : previewableViewMode
    : "source";
  const viewMode: "editor" | "preview" | "source" | "diff" =
    diffActive && isDiffAvailable ? "diff" : fileViewMode;
  const diffViewActive = viewMode === "diff";

  useEffect(() => {
    const el = contentAreaRef.current;
    if (!el || !diffViewActive || typeof ResizeObserver === "undefined") return;
    const measure = () => setContentWidth(el.getBoundingClientRect().width);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [diffViewActive]);

  const splitToggleAvailable =
    contentWidth === null || contentWidth === 0 || contentWidth >= MONACO_SPLIT_BREAKPOINT;

  useEffect(() => {
    if (viewMode !== "editor") setIsEditorDirty(false);
    if (viewModeInitializedRef.current && (viewMode === "editor" || viewMode === "preview")) {
      setActiveSelection(null);
    }
    viewModeInitializedRef.current = true;
  }, [viewMode]);

  useEffect(() => {
    if (!open) return;
    const wantDiff = diffActive && isDiffAvailable;
    const hasDiff = searchParams.has("diff");
    if (wantDiff === hasDiff) return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (wantDiff) {
          next.set("diff", "1");
        } else {
          next.delete("diff");
        }
        return next;
      },
      { replace: true },
    );
  }, [diffActive, isDiffAvailable, open]); // eslint-disable-line react-hooks/exhaustive-deps

  const showNavButtons = currentNavIdx !== -1 && navigableFiles.length > 1 && !!onNavigateTo;
  const toolbarActions = buildFileViewerToolbarActions({
    isPreviewable,
    lang,
    viewMode,
    commentsOpen,
    isDiffAvailable,
    splitToggleAvailable,
    diffLayout,
    isDeletedFile,
    fileQuery,
    linkCopied,
    guardDirty,
    setPreviewableViewMode,
    onCommentsToggle: () => {
      commentsInitializedRef.current = true;
      setCommentsOpen((prev) => !prev);
    },
    setDiffActive,
    setDiffLayout,
    openSearch,
    downloadFile,
    copyFileLink,
  });

  return {
    canEdit,
    panelWidth,
    handleProps,
    isDesktop,
    fileQuery,
    diffQuery,
    navigableFiles,
    currentNavIdx,
    prevPath,
    nextPath,
    activeSelection,
    contentAreaRef,
    pendingBodyRef,
    isEditorDirty,
    setIsEditorDirty,
    saveStatus,
    setSaveStatus,
    pendingAction,
    setPendingAction,
    guardDirty,
    handleSetActiveSelection,
    copyCommentLink,
    searchOpen,
    setSearchOpen,
    searchInputRef,
    viewMode,
    isDiffAvailable,
    isDeletedFile,
    openComments,
    addressedComments,
    commentsOpen,
    showNavButtons,
    toolbarActions,
    setSearchParams,
    diffLayout,
  };
}