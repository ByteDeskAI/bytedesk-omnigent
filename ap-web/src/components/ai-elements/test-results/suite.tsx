"use client";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { ChevronRightIcon } from "lucide-react";
import type { ComponentProps, HTMLAttributes } from "react";
import { useContext, useMemo } from "react";
import { TestStatusIcon } from "./constants";
import { TestSuiteContext } from "./context";
import type { TestStatusKind } from "./types";

export type TestSuiteProps = ComponentProps<typeof Collapsible> & {
  name: string;
  status: TestStatusKind;
};

export const TestSuite = ({ name, status, className, children, ...props }: TestSuiteProps) => {
  const contextValue = useMemo(() => ({ name, status }), [name, status]);

  return (
    <TestSuiteContext.Provider value={contextValue}>
      <Collapsible className={cn("rounded-lg border", className)} {...props}>
        {children}
      </Collapsible>
    </TestSuiteContext.Provider>
  );
};

export type TestSuiteNameProps = ComponentProps<typeof CollapsibleTrigger>;

export const TestSuiteName = ({ className, children, ...props }: TestSuiteNameProps) => {
  const { name, status } = useContext(TestSuiteContext);

  return (
    <CollapsibleTrigger
      className={cn(
        "group flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-muted/50",
        className,
      )}
      {...props}
    >
      <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
      <TestStatusIcon status={status} />
      <span className="font-medium text-sm">{children ?? name}</span>
    </CollapsibleTrigger>
  );
};

export type TestSuiteStatsProps = HTMLAttributes<HTMLDivElement> & {
  passed?: number;
  failed?: number;
  skipped?: number;
};

export const TestSuiteStats = ({
  passed = 0,
  failed = 0,
  skipped = 0,
  className,
  children,
  ...props
}: TestSuiteStatsProps) => (
  <div className={cn("ml-auto flex items-center gap-2 text-xs", className)} {...props}>
    {children ?? (
      <>
        {passed > 0 && <span className="text-green-600 dark:text-green-400">{passed} passed</span>}
        {failed > 0 && <span className="text-red-600 dark:text-red-400">{failed} failed</span>}
        {skipped > 0 && (
          <span className="text-yellow-600 dark:text-yellow-400">{skipped} skipped</span>
        )}
      </>
    )}
  </div>
);

export type TestSuiteContentProps = ComponentProps<typeof CollapsibleContent>;

export const TestSuiteContent = ({ className, children, ...props }: TestSuiteContentProps) => (
  <CollapsibleContent className={cn("border-t", className)} {...props}>
    <div className="divide-y">{children}</div>
  </CollapsibleContent>
);