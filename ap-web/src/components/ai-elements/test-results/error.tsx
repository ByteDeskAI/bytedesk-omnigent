"use client";

import { cn } from "@/lib/utils";
import type { HTMLAttributes } from "react";

export type TestErrorProps = HTMLAttributes<HTMLDivElement>;

export const TestError = ({ className, children, ...props }: TestErrorProps) => (
  <div className={cn("mt-2 rounded-md bg-red-50 p-3 dark:bg-red-900/20", className)} {...props}>
    {children}
  </div>
);

export type TestErrorMessageProps = HTMLAttributes<HTMLParagraphElement>;

export const TestErrorMessage = ({ className, children, ...props }: TestErrorMessageProps) => (
  <p className={cn("font-medium text-red-700 text-sm dark:text-red-400", className)} {...props}>
    {children}
  </p>
);

export type TestErrorStackProps = HTMLAttributes<HTMLPreElement>;

export const TestErrorStack = ({ className, children, ...props }: TestErrorStackProps) => (
  <pre
    className={cn("mt-2 overflow-auto font-mono text-red-600 text-xs dark:text-red-400", className)}
    {...props}
  >
    {children}
  </pre>
);