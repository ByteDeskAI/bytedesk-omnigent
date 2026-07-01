"use client";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { ChevronDownIcon } from "lucide-react";
import type { ComponentProps } from "react";
import { useMemo } from "react";
import { OpenInContext } from "./context";

export type OpenInProps = ComponentProps<typeof DropdownMenu> & {
  query: string;
};

export const OpenIn = ({ query, ...props }: OpenInProps) => {
  const contextValue = useMemo(() => ({ query }), [query]);

  return (
    <OpenInContext.Provider value={contextValue}>
      <DropdownMenu {...props} />
    </OpenInContext.Provider>
  );
};

export type OpenInContentProps = ComponentProps<typeof DropdownMenuContent>;

export const OpenInContent = ({ className, ...props }: OpenInContentProps) => (
  <DropdownMenuContent align="start" className={cn("w-[240px]", className)} {...props} />
);

export type OpenInItemProps = ComponentProps<typeof DropdownMenuItem>;

export const OpenInItem = (props: OpenInItemProps) => <DropdownMenuItem {...props} />;

export type OpenInLabelProps = ComponentProps<typeof DropdownMenuLabel>;

export const OpenInLabel = (props: OpenInLabelProps) => <DropdownMenuLabel {...props} />;

export type OpenInSeparatorProps = ComponentProps<typeof DropdownMenuSeparator>;

export const OpenInSeparator = (props: OpenInSeparatorProps) => (
  <DropdownMenuSeparator {...props} />
);

export type OpenInTriggerProps = ComponentProps<typeof DropdownMenuTrigger>;

export const OpenInTrigger = ({ children, ...props }: OpenInTriggerProps) => (
  <DropdownMenuTrigger {...props} asChild>
    {children ?? (
      <Button type="button" variant="outline">
        Open in chat
        <ChevronDownIcon className="size-4" />
      </Button>
    )}
  </DropdownMenuTrigger>
);