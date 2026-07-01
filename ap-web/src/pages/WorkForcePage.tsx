import { WorkForcePageShell } from "./organisms/WorkForcePageShell";
import { useWorkForcePage } from "./useWorkForcePage";

export function WorkForcePage() {
  return <WorkForcePageShell {...useWorkForcePage()} />;
}