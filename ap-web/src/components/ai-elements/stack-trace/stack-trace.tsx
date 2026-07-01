"use client";

import { useControllableState } from "@radix-ui/react-use-controllable-state";
import { cn } from "@/lib/utils";
import type { ComponentProps } from "react";
import { memo, useMemo } from "react";

import { StackTraceContext } from "./context";
import { parseStackTrace } from "./parse";

export type StackTraceProps = ComponentProps<"div"> & {
  trace: string;
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  onFilePathClick?: (filePath: string, line?: number, column?: number) => void;
};

export const StackTrace = memo(
  ({
    trace,
    className,
    open,
    defaultOpen = false,
    onOpenChange,
    onFilePathClick,
    children,
    ...props
  }: StackTraceProps) => {
    const [isOpen, setIsOpen] = useControllableState({
      defaultProp: defaultOpen,
      onChange: onOpenChange,
      prop: open,
    });

    const parsedTrace = useMemo(() => parseStackTrace(trace), [trace]);

    const contextValue = useMemo(
      () => ({
        isOpen,
        onFilePathClick,
        raw: trace,
        setIsOpen,
        trace: parsedTrace,
      }),
      [parsedTrace, trace, isOpen, setIsOpen, onFilePathClick],
    );

    return (
      <StackTraceContext.Provider value={contextValue}>
        <div
          className={cn(
            "not-prose w-full overflow-hidden rounded-lg border bg-background font-mono text-sm",
            className,
          )}
          {...props}
        >
          {children}
        </div>
      </StackTraceContext.Provider>
    );
  },
);

StackTrace.displayName = "StackTrace";