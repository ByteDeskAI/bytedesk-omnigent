"use client";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import type { ComponentProps } from "react";

export type CommitProps = ComponentProps<typeof Collapsible>;

export const Commit = ({ className, children, ...props }: CommitProps) => (
  <Collapsible className={cn("rounded-lg border bg-background", className)} {...props}>
    {children}
  </Collapsible>
);

export type CommitHeaderProps = ComponentProps<typeof CollapsibleTrigger>;

export const CommitHeader = ({ className, children, ...props }: CommitHeaderProps) => (
  <CollapsibleTrigger asChild {...props}>
    <div
      className={cn(
        "group flex cursor-pointer items-center justify-between gap-4 p-3 text-left transition-colors hover:opacity-80",
        className,
      )}
    >
      {children}
    </div>
  </CollapsibleTrigger>
);

export type CommitContentProps = ComponentProps<typeof CollapsibleContent>;

export const CommitContent = ({ className, children, ...props }: CommitContentProps) => (
  <CollapsibleContent className={cn("border-t p-3", className)} {...props}>
    {children}
  </CollapsibleContent>
);