import { ChevronDownIcon, ChevronUpIcon } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ConnectHostInstructions } from "../new-chat-landing/ConnectHostInstructions";
import { ForkSessionHostLabel } from "./ForkSessionHostLabel";
import type { Host } from "@/hooks/useHosts";

export function ForkSessionHostSection({
  hosts,
  allHosts,
  onlineHosts,
  offlineHosts,
  selectedHostId,
  setSelectedHostId,
  onHostChange,
  serverUrl,
  showConnect,
  setShowConnect,
}: {
  hosts: Host[] | undefined;
  allHosts: Host[];
  onlineHosts: Host[];
  offlineHosts: Host[];
  selectedHostId: string | null;
  setSelectedHostId: (id: string) => void;
  onHostChange: () => void;
  serverUrl: string;
  showConnect: boolean;
  setShowConnect: (v: boolean | ((prev: boolean) => boolean)) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs font-medium text-muted-foreground">Host</span>
      {hosts === undefined ? (
        <p className="text-xs text-muted-foreground" data-testid="fork-session-no-hosts">
          Loading hosts…
        </p>
      ) : onlineHosts.length === 0 ? (
        <ConnectHostInstructions
          serverUrl={serverUrl}
          label={
            allHosts.length === 0
              ? "No hosts connected yet. Connect one from your terminal:"
              : "No hosts online. Reconnect from your terminal to start the clone:"
          }
        />
      ) : (
        <>
          <Select
            value={selectedHostId ?? ""}
            onValueChange={(v) => {
              setSelectedHostId(v);
              onHostChange();
            }}
          >
            <SelectTrigger className="w-full text-xs" data-testid="fork-session-host-select">
              <SelectValue placeholder="Select a host" />
            </SelectTrigger>
            <SelectContent>
              {onlineHosts.map((host) => (
                <SelectItem
                  key={host.host_id}
                  value={host.host_id}
                  data-testid={`fork-session-host-option-${host.host_id}`}
                >
                  <ForkSessionHostLabel host={host} />
                </SelectItem>
              ))}
              {offlineHosts.map((host) => (
                <SelectItem
                  key={host.host_id}
                  value={host.host_id}
                  disabled
                  data-testid={`fork-session-host-option-${host.host_id}`}
                >
                  <ForkSessionHostLabel host={host} />
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <button
            type="button"
            onClick={() => setShowConnect((v) => !v)}
            className="flex cursor-pointer items-center gap-1 self-start text-xs text-muted-foreground transition hover:text-foreground"
            data-testid="fork-session-connect-host-toggle"
          >
            {showConnect ? (
              <ChevronUpIcon className="size-3.5" />
            ) : (
              <ChevronDownIcon className="size-3.5" />
            )}
            Connect another host from your terminal
          </button>
          {showConnect && <ConnectHostInstructions serverUrl={serverUrl} />}
        </>
      )}
    </div>
  );
}