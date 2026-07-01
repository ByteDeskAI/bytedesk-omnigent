"use client";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { CheckIcon, ChevronDownIcon, CopyIcon } from "lucide-react";
import type { ComponentProps } from "react";
import { memo, useCallback, useEffect, useRef, useState } from "react";

import { useStackTrace } from "./context";

export type StackTraceActionsProps = ComponentProps<"div">;

const handleActionsClick = (e: React.MouseEvent) => e.stopPropagation();
const handleActionsKeyDown = (e: React.KeyboardEvent) => {
  if (e.key === "Enter" || e.key === " ") {
    e.stopPropagation();
  }
};

export const StackTraceActions = memo(
  ({ className, children, ...props }: StackTraceActionsProps) => (
    <div
      className={cn("flex shrink-0 items-center gap-1", className)}
      onClick={handleActionsClick}
      onKeyDown={handleActionsKeyDown}
      role="group"
      {...props}
    >
      {children}
    </div>
  ),
);

export type StackTraceCopyButtonProps = ComponentProps<typeof Button> & {
  onCopy?: () => void;
  onError?: (error: Error) => void;
  timeout?: number;
};

export const StackTraceCopyButton = memo(
  ({
    onCopy,
    onError,
    timeout = 2000,
    className,
    children,
    ...props
  }: StackTraceCopyButtonProps) => {
    const [isCopied, setIsCopied] = useState(false);
    const timeoutRef = useRef<number>(0);
    const { raw } = useStackTrace();

    const copyToClipboard = useCallback(async () => {
      if (typeof window === "undefined" || !navigator?.clipboard?.writeText) {
        onError?.(new Error("Clipboard API not available"));
        return;
      }

      try {
        await navigator.clipboard.writeText(raw);
        setIsCopied(true);
        onCopy?.();
        timeoutRef.current = window.setTimeout(() => setIsCopied(false), timeout);
      } catch (error) {
        onError?.(error as Error);
      }
    }, [raw, onCopy, onError, timeout]);

    useEffect(
      () => () => {
        window.clearTimeout(timeoutRef.current);
      },
      [],
    );

    const Icon = isCopied ? CheckIcon : CopyIcon;

    return (
      <Button
        className={cn("size-7", className)}
        onClick={copyToClipboard}
        size="icon"
        variant="ghost"
        {...props}
      >
        {children ?? <Icon size={14} />}
      </Button>
    );
  },
);

export type StackTraceExpandButtonProps = ComponentProps<"div">;

export const StackTraceExpandButton = memo(
  ({ className, ...props }: StackTraceExpandButtonProps) => {
    const { isOpen } = useStackTrace();

    return (
      <div className={cn("flex size-7 items-center justify-center", className)} {...props}>
        <ChevronDownIcon
          className={cn(
            "size-4 text-muted-foreground transition-transform",
            isOpen ? "rotate-180" : "rotate-0",
          )}
        />
      </div>
    );
  },
);

StackTraceActions.displayName = "StackTraceActions";
StackTraceCopyButton.displayName = "StackTraceCopyButton";
StackTraceExpandButton.displayName = "StackTraceExpandButton";