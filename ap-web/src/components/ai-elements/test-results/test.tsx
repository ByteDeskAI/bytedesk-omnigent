"use client";

import { cn } from "@/lib/utils";
import type { HTMLAttributes } from "react";
import { useContext, useMemo } from "react";
import { statusIcons, statusStyles } from "./constants";
import { TestContext } from "./context";
import type { TestStatusKind } from "./types";

export type TestNameProps = HTMLAttributes<HTMLSpanElement>;

export const TestName = ({ className, children, ...props }: TestNameProps) => {
  const { name } = useContext(TestContext);

  return (
    <span className={cn("flex-1", className)} {...props}>
      {children ?? name}
    </span>
  );
};

export type TestDurationProps = HTMLAttributes<HTMLSpanElement>;

export const TestDuration = ({ className, children, ...props }: TestDurationProps) => {
  const { duration } = useContext(TestContext);

  if (duration === undefined) {
    return null;
  }

  return (
    <span className={cn("ml-auto text-muted-foreground text-xs", className)} {...props}>
      {children ?? `${duration}ms`}
    </span>
  );
};

export type TestStatusProps = HTMLAttributes<HTMLSpanElement>;

export const TestStatus = ({ className, children, ...props }: TestStatusProps) => {
  const { status } = useContext(TestContext);

  return (
    <span className={cn("shrink-0", statusStyles[status], className)} {...props}>
      {children ?? statusIcons[status]}
    </span>
  );
};

export type TestProps = HTMLAttributes<HTMLDivElement> & {
  name: string;
  status: TestStatusKind;
  duration?: number;
};

export const Test = ({ name, status, duration, className, children, ...props }: TestProps) => {
  const contextValue = useMemo(() => ({ duration, name, status }), [duration, name, status]);

  return (
    <TestContext.Provider value={contextValue}>
      <div className={cn("flex items-center gap-2 px-4 py-2 text-sm", className)} {...props}>
        {children ?? (
          <>
            <TestStatus />
            <TestName />
            {duration !== undefined && <TestDuration />}
          </>
        )}
      </div>
    </TestContext.Provider>
  );
};