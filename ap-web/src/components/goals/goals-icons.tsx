import {
  AlertTriangleIcon,
  BotIcon,
  Building2Icon,
  CheckCircle2Icon,
  CircleDashedIcon,
  CircleDotIcon,
  NetworkIcon,
  PauseCircleIcon,
  TargetIcon,
} from "lucide-react";
import type { GoalRecord } from "@/lib/goalsApi";
import type { ScopeKind } from "./goals-utils";

export function iconForScope(kind: ScopeKind, className = "size-4") {
  if (kind === "organization") return <Building2Icon className={className} />;
  if (kind === "department") return <NetworkIcon className={className} />;
  if (kind === "agent") return <BotIcon className={className} />;
  return <TargetIcon className={className} />;
}

export function activationIcon(goal: GoalRecord) {
  if (goal.status === "blocked") return <AlertTriangleIcon className="size-3.5" />;
  if (goal.status === "done") return <CheckCircle2Icon className="size-3.5" />;
  if (goal.activation_state === "paused") return <PauseCircleIcon className="size-3.5" />;
  if (goal.activation_state === "waiting") return <CircleDashedIcon className="size-3.5" />;
  return <CircleDotIcon className="size-3.5" />;
}

export function milestoneIcon(status?: string) {
  if (status === "done") return <CheckCircle2Icon className="size-3.5" />;
  if (status === "awaiting_pr" || status === "awaiting_jira")
    return <AlertTriangleIcon className="size-3.5" />;
  if (status === "in_progress") return <CircleDotIcon className="size-3.5" />;
  return <CircleDashedIcon className="size-3.5" />;
}