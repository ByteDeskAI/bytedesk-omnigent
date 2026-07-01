import {
  CircleHelpIcon,
  ChevronDownIcon,
  MonitorCloudIcon,
  MonitorIcon,
  PlusIcon,
} from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { NewChatHostOption } from "./NewChatHostOption";
import type { NewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingHostChip({ state }: { state: NewChatLandingState }) {
  const {
    sandboxSelected,
    isCloudHost,
    selectedHost,
    onlineHosts,
    hostLabel,
    managedSandboxesEnabled,
    showDisabledSandboxWithDocs,
    sandboxLabel,
    selectSandbox,
    selectHost,
    allHosts,
    offlineHosts,
    newSandboxTooltipContent,
    setConnectOpen,
    selectedHostId,
  } = state;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="flex h-6 items-center gap-1.5 rounded-full px-3 text-13 font-normal text-muted-foreground transition-colors hover:text-foreground"
          data-testid="new-chat-landing-host-chip"
        >
          {isCloudHost ? (
            <MonitorCloudIcon className="size-4 shrink-0" />
          ) : (
            <MonitorIcon className="size-4 shrink-0" />
          )}
          <span
            className={`max-w-32 truncate ${sandboxSelected || selectedHost != null ? "text-foreground" : ""}`}
          >
            {hostLabel}
          </span>
          <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-52">
        {(managedSandboxesEnabled || showDisabledSandboxWithDocs) && (
          <>
            {managedSandboxesEnabled ? (
              <DropdownMenuItem
                onSelect={selectSandbox}
                data-testid="new-chat-landing-sandbox-option"
                data-active={sandboxSelected ? "true" : undefined}
                className="text-xs data-[active=true]:bg-accent/60"
              >
                <span className="flex items-center gap-2">
                  <MonitorCloudIcon className="size-4 text-muted-foreground" />
                  <span className="text-xs">{sandboxLabel}</span>
                </span>
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem
                aria-disabled="true"
                onSelect={(e) => e.preventDefault()}
                className="flex items-center justify-between px-2 py-1.5 text-xs text-muted-foreground opacity-60"
                data-testid="new-chat-landing-sandbox-option-disabled"
              >
                <span className="flex items-center gap-2">
                  <MonitorCloudIcon className="size-4 text-muted-foreground" />
                  <span className="text-xs">New Sandbox</span>
                </span>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      className="inline-flex size-4 items-center justify-center rounded-sm text-muted-foreground/80 hover:text-foreground"
                      aria-label="Why New Sandbox is unavailable"
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") e.stopPropagation();
                      }}
                    >
                      <CircleHelpIcon className="size-3.5" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-64">{newSandboxTooltipContent}</TooltipContent>
                </Tooltip>
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
          </>
        )}
        {allHosts.length === 0 && (
          <div className="px-2 py-1.5 text-xs text-muted-foreground">No hosts connected yet.</div>
        )}
        {onlineHosts.map((host) => (
          <DropdownMenuItem
            key={host.host_id}
            onSelect={() => selectHost(host.host_id)}
            data-active={host.host_id === selectedHostId ? "true" : undefined}
            className="text-xs data-[active=true]:bg-accent/60"
          >
            <NewChatHostOption host={host} />
          </DropdownMenuItem>
        ))}
        {offlineHosts.map((host) => (
          <DropdownMenuItem key={host.host_id} disabled className="text-xs">
            <NewChatHostOption host={host} />
          </DropdownMenuItem>
        ))}
        {allHosts.length > 0 && <DropdownMenuSeparator />}
        <DropdownMenuItem
          onSelect={() => setConnectOpen(true)}
          data-testid="new-chat-landing-connect-host"
          className="gap-2 text-xs text-muted-foreground"
        >
          <PlusIcon className="size-3.5" />
          Connect new host
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}