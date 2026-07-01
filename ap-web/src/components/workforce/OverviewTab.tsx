import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { AgentTier } from "@/lib/agentTiers";
import { InfoRow } from "./InfoRow";
import { agentDisplayName } from "./workforce-utils";

export function OverviewTab({
  agent,
  tier,
  imageVersion,
  sotTier,
  imageLoaded,
}: {
  agent: AvailableAgent;
  tier: AgentTier;
  imageVersion: number | null;
  sotTier: string | null;
  imageLoaded: boolean;
}) {
  return (
    <div className="mc-fade-up grid gap-4 p-4 xl:grid-cols-2">
      <section className="mc-surface p-4">
        <h3 className="mc-label mb-3">Identity</h3>
        <dl className="grid gap-2 text-sm">
          <InfoRow label="Name" value={agent.name} />
          <InfoRow label="Display" value={agentDisplayName(agent)} />
          <InfoRow label="Category" value={tier} />
          <InfoRow label="Department" value={agent.department || "Unassigned"} />
          <InfoRow label="Title" value={agent.title || "None"} />
        </dl>
      </section>
      <section className="mc-surface p-4">
        <h3 className="mc-label mb-3">Image</h3>
        <dl className="grid gap-2 text-sm">
          <InfoRow label="Editable image" value={imageLoaded ? "Available" : "Unavailable"} />
          <InfoRow label="Version" value={imageVersion === null ? "Unknown" : imageVersion} />
          <InfoRow label="Source tier" value={sotTier || "default"} />
          <InfoRow label="Harness" value={agent.harness || "unknown"} />
          <InfoRow label="Bundled skills" value={agent.skills.length} />
        </dl>
      </section>
    </div>
  );
}