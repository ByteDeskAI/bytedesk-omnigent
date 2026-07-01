import {
  CheckCircle2Icon,
  CircleDotIcon,
  CircleIcon,
  XCircleIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { TestStatusKind } from "./types";

export const formatDuration = (ms: number) => {
  if (ms < 1000) {
    return `${ms}ms`;
  }
  return `${(ms / 1000).toFixed(2)}s`;
};

export const statusStyles: Record<TestStatusKind, string> = {
  failed: "text-red-600 dark:text-red-400",
  passed: "text-green-600 dark:text-green-400",
  running: "text-blue-600 dark:text-blue-400",
  skipped: "text-yellow-600 dark:text-yellow-400",
};

export const statusIcons: Record<TestStatusKind, React.ReactNode> = {
  failed: <XCircleIcon className="size-4" />,
  passed: <CheckCircle2Icon className="size-4" />,
  running: <CircleDotIcon className="size-4 animate-pulse" />,
  skipped: <CircleIcon className="size-4" />,
};

export function TestStatusIcon({ status }: { status: TestStatusKind }) {
  return <span className={cn("shrink-0", statusStyles[status])}>{statusIcons[status]}</span>;
}