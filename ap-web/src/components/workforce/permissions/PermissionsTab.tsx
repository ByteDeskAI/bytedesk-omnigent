import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { PermissionsTabView } from "./PermissionsTabView";
import { usePermissionsTab } from "./usePermissionsTab";

export function PermissionsTab({ agent, editable }: { agent: AvailableAgent; editable: boolean }) {
  return <PermissionsTabView {...usePermissionsTab(agent, editable)} />;
}
