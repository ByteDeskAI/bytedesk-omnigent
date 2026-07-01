import type { Agent } from "@/hooks/useAgents";
import type { CostRoutingVerdict } from "@/components/CostRoutingControl";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import type { Bubble } from "@/lib/renderItems";

export interface MainAgentSurfaceProps {
  conversationId: string | null;
  bubbles: Bubble[];
  status: "idle" | "streaming";
  isWorking: boolean;
  showsWorking: boolean;
  runnerOnline: boolean | undefined;
  liveness: SessionLiveness;
  agentsError: unknown;
  disabled: boolean;
  onSend: (text: string, files?: File[]) => void;
  onSendSlashCommand?: (name: string, args: string) => void;
  onStop: () => void;
  onShowReconnectHelp: () => void;
  agents: Agent[] | undefined;
  agentsLoading: boolean;
  selectedAgentId: string | null;
  onSelectAgent: (id: string) => void;
  hasMoreHistory: boolean;
  loadingMoreHistory: boolean;
  permissionLevel: number | null;
  readOnlyReason: string | null;
  effortLevels: readonly string[];
  showEffort: boolean;
  showModels: boolean;
  costRoutingVerdict: CostRoutingVerdict | null;
  costRoutingEligible: boolean;
  subAgentLabel: string | null;
}