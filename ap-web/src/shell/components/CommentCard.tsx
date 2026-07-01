import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { CheckIcon, Link2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { Comment } from "@/hooks/useComments";
import { avatarStyle, formatCommentTime } from "./commentsPanelUtils";

interface CommentCardProps {
  comment: Comment;
  isSelected?: boolean;
  onClick?: () => void;
  onEdit?: (body: string) => void;
  onDelete?: () => void;
  onCopyLink?: () => void;
}

export function CommentCard({
  comment: c,
  isSelected,
  onClick,
  onEdit,
  onDelete,
  onCopyLink,
}: CommentCardProps) {
  const [editing, setEditing] = useState(false);
  const [editBody, setEditBody] = useState(c.body);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [linkCopied, setLinkCopied] = useState(false);
  const linkCopiedTimerRef = useRef<number>(0);

  const bodyRef = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(false);

  useEffect(
    () => () => {
      window.clearTimeout(linkCopiedTimerRef.current);
    },
    [],
  );

  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el || editing || expanded) return;
    const measure = () => setClamped(el.scrollHeight > el.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [c.body, editing, expanded]);

  useEffect(() => {
    setExpanded(false);
  }, [c.id]);

  useEffect(() => {
    if (!editing) setEditBody(c.body);
  }, [c.id, c.body, editing]);

  const statusLabel = c.status === "addressed" ? "Addressed" : null;

  function startEdit() {
    setEditBody(c.body);
    setEditing(true);
    setTimeout(() => textareaRef.current?.focus(), 0);
  }

  function saveEdit() {
    if (editBody.trim()) onEdit?.(editBody.trim());
    setEditing(false);
  }

  return (
    <div
      className={cn(
        "rounded-lg border p-3 space-y-2 transition-colors",
        isSelected
          ? "border-primary bg-primary/10 ring-1 ring-primary/30 cursor-default"
          : "border-border bg-muted/20 cursor-pointer hover:border-foreground/20",
      )}
      onClick={() => {
        if (!editing) onClick?.();
      }}
    >
      {c.anchor_content && (
        <p className="truncate font-mono text-[11px] text-muted-foreground">
          {c.anchor_content.trim()}
        </p>
      )}

      {editing ? (
        <div className="space-y-1.5">
          <textarea
            ref={textareaRef}
            className="w-full resize-none rounded border border-border bg-background px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
            rows={3}
            value={editBody}
            onChange={(e) => setEditBody(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) saveEdit();
              if (e.key === "Escape") setEditing(false);
            }}
          />
          <div className="flex gap-1.5">
            <Button type="button" size="xs" disabled={!editBody.trim()} onClick={saveEdit}>
              Save
            </Button>
            <Button type="button" size="xs" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <p
            ref={bodyRef}
            className={cn(
              "text-xs leading-relaxed text-foreground break-words whitespace-pre-wrap",
              !expanded && "line-clamp-4",
            )}
          >
            {c.body}
          </p>
          {(clamped || expanded) && (
            <button
              type="button"
              aria-expanded={expanded}
              className="cursor-pointer text-[10px] font-medium text-blue-600 hover:underline dark:text-blue-400"
              onClick={(e) => {
                e.stopPropagation();
                setExpanded((v) => !v);
              }}
            >
              {expanded ? "Show less" : "Show more"}
            </button>
          )}
        </div>
      )}

      {!editing && (
        <div className="flex items-end justify-between gap-2">
          <div className="flex min-w-0 flex-col gap-0.5">
            <div className="flex min-w-0 items-center gap-1.5">
              <span
                className="inline-flex size-4 shrink-0 items-center justify-center rounded-full text-[8px] font-semibold uppercase"
                style={avatarStyle(c.created_by ?? "You")}
              >
                {(c.created_by ?? "Y")[0].toUpperCase()}
              </span>
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="truncate text-[11px] text-muted-foreground">
                      {c.created_by ?? "You"}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>{c.created_by ?? "You"}</TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </div>
            <span className="text-[10px] text-muted-foreground/70">
              {formatCommentTime(c.created_at)}
            </span>
            {statusLabel && (
              <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] w-fit">
                {statusLabel}
              </span>
            )}
          </div>
          {(onEdit || onDelete || onCopyLink) && (
            <div className="flex shrink-0 items-center gap-2 mr-0.5">
              {onEdit && (
                <button
                  type="button"
                  className="cursor-pointer text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                  onClick={(e) => {
                    e.stopPropagation();
                    startEdit();
                  }}
                >
                  Edit
                </button>
              )}
              {onDelete && (
                <button
                  type="button"
                  className="cursor-pointer text-[11px] text-muted-foreground transition-colors hover:text-destructive"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete();
                  }}
                >
                  Delete
                </button>
              )}
              {onCopyLink && (
                <button
                  type="button"
                  aria-label="Copy link to comment"
                  className="cursor-pointer text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                  onClick={(e) => {
                    e.stopPropagation();
                    onCopyLink();
                    setLinkCopied(true);
                    window.clearTimeout(linkCopiedTimerRef.current);
                    linkCopiedTimerRef.current = window.setTimeout(
                      () => setLinkCopied(false),
                      2000,
                    );
                  }}
                >
                  {linkCopied ? <CheckIcon className="size-3" /> : <Link2Icon className="size-3" />}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}