import { BotIcon } from "lucide-react";
import { Link, useLocation } from "@/lib/routing";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { NessieIcon } from "@/components/icons/NessieIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import { useSession } from "@/hooks/useSession";
import { cn } from "@/lib/utils";
import { nativeCodingAgentForWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import { mainMessagePreview, railLinkSearch, sessionStatus } from "./subagentsPanelUtils";
import { SubagentStatusIndicator } from "./SubagentStatusIndicator";

export function SubagentMainRow({
  rootSessionId,
  isActive,
}: {
  rootSessionId: string;
  isActive: boolean;
}) {
  const { session } = useSession(rootSessionId);
  const search = railLinkSearch(useLocation().search);
  const wrapper = session?.labels?.[WRAPPER_LABEL_KEY];
  const nativeAgent = nativeCodingAgentForWrapper(wrapper);
  const isNessie = session?.agentName === "nessie";
  const Icon =
    nativeAgent?.iconKind === "claude"
      ? ClaudeIcon
      : nativeAgent?.iconKind === "codex"
        ? CodexIcon
        : nativeAgent?.iconKind === "pi"
          ? PiIcon
          : isNessie
            ? NessieIcon
            : BotIcon;
  const label = nativeAgent?.displayName ?? session?.agentName ?? "main";
  const preview = mainMessagePreview(session?.items);
  return (
    <li>
      <Link
        to={{ pathname: `/c/${rootSessionId}`, search }}
        data-testid="subagent-main-row"
        data-root-session-id={rootSessionId}
        data-agent-kind={
          nativeAgent != null ? `${nativeAgent.key}-native` : isNessie ? "nessie" : "agent"
        }
        className={cn(
          "flex w-full flex-col gap-0.5 px-2.5 py-2 text-left hover:bg-accent/60",
          isActive && "bg-accent",
        )}
      >
        <div className="flex w-full items-center gap-1">
          <Icon className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="shrink-0 truncate text-xs font-medium">{label}</span>
          <span className="flex-1" />
          <SubagentStatusIndicator {...sessionStatus(session?.status)} />
        </div>
        {preview && (
          <p
            data-testid="subagent-main-preview"
            className="truncate pl-[18px] text-[11px] text-muted-foreground"
          >
            {preview}
          </p>
        )}
      </Link>
    </li>
  );
}