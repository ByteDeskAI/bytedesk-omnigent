"use client";

import { Collapsible, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { AlertTriangleIcon } from "lucide-react";
import type { ComponentProps } from "react";
import { memo } from "react";

import { useStackTrace } from "./context";

export type StackTraceHeaderProps = ComponentProps<typeof CollapsibleTrigger>;

export const StackTraceHeader = memo(({ className, children, ...props }: StackTraceHeaderProps) => {
  const { isOpen, setIsOpen } = useStackTrace();

  return (
    <Collapsible onOpenChange={setIsOpen} open={isOpen}>
      <CollapsibleTrigger asChild {...props}>
        <div
          className={cn(
            "flex w-full cursor-pointer items-center gap-3 p-3 text-left transition-colors hover:bg-muted/50",
            className,
          )}
        >
          {children}
        </div>
      </CollapsibleTrigger>
    </Collapsible>
  );
});

export type StackTraceErrorProps = ComponentProps<"div">;

export const StackTraceError = memo(({ className, children, ...props }: StackTraceErrorProps) => (
  <div className={cn("flex flex-1 items-center gap-2 overflow-hidden", className)} {...props}>
    <AlertTriangleIcon className="size-4 shrink-0 text-destructive" />
    {children}
  </div>
));

export type StackTraceErrorTypeProps = ComponentProps<"span">;

export const StackTraceErrorType = memo(
  ({ className, children, ...props }: StackTraceErrorTypeProps) => {
    const { trace } = useStackTrace();

    return (
      <span className={cn("shrink-0 font-semibold text-destructive", className)} {...props}>
        {children ?? trace.errorType}
      </span>
    );
  },
);

export type StackTraceErrorMessageProps = ComponentProps<"span">;

export const StackTraceErrorMessage = memo(
  ({ className, children, ...props }: StackTraceErrorMessageProps) => {
    const { trace } = useStackTrace();

    return (
      <span className={cn("truncate text-foreground", className)} {...props}>
        {children ?? trace.errorMessage}
      </span>
    );
  },
);

StackTraceHeader.displayName = "StackTraceHeader";
StackTraceError.displayName = "StackTraceError";
StackTraceErrorType.displayName = "StackTraceErrorType";
StackTraceErrorMessage.displayName = "StackTraceErrorMessage";