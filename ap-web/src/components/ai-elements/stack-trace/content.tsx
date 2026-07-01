"use client";

import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import type { ComponentProps } from "react";
import { memo } from "react";

import { useStackTrace } from "./context";

export type StackTraceContentProps = ComponentProps<typeof CollapsibleContent> & {
  maxHeight?: number;
};

export const StackTraceContent = memo(
  ({ className, maxHeight = 400, children, ...props }: StackTraceContentProps) => {
    const { isOpen } = useStackTrace();

    return (
      <Collapsible open={isOpen}>
        <CollapsibleContent
          className={cn(
            "overflow-auto border-t bg-muted/30",
            "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in",
            className,
          )}
          style={{ maxHeight }}
          {...props}
        >
          {children}
        </CollapsibleContent>
      </Collapsible>
    );
  },
);

StackTraceContent.displayName = "StackTraceContent";