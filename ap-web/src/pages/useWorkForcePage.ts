import { useEffect, useMemo, useState } from "react";
import {
  agentDisplayName,
  compareAgentsByName,
  groupEmployeesByDepartment,
  tierRequiresEditConfirmation,
  useWorkForceAdminAccess,
  workForceRosterAgents,
  type PendingSave,
  type WorkForceTab,
} from "@/components/workforce";
import { useAgentImage, useUpdateAgentImage } from "@/hooks/useAgentImages";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import type { AgentImageUpdate } from "@/lib/agentImagesApi";
import { groupAgentsByTier, tierForAgent } from "@/lib/agentTiers";
import type { WorkForcePageShellProps } from "./organisms/WorkForcePageShell";

export function useWorkForcePage(): WorkForcePageShellProps {
  const allowed = useWorkForceAdminAccess();
  const agentsQuery = useAvailableAgents({ includeSessionAgents: false });
  const updateImage = useUpdateAgentImage();
  const [query, setQuery] = useState("");
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [tab, setTab] = useState<WorkForceTab>("overview");
  const [configText, setConfigText] = useState("");
  const [instructionsText, setInstructionsText] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveNotice, setSaveNotice] = useState<string | null>(null);
  const [pendingSave, setPendingSave] = useState<PendingSave | null>(null);

  const filteredAgents = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const rows = workForceRosterAgents(agentsQuery.data ?? []);
    if (!needle) return rows;
    return rows.filter((agent) =>
      [agent.name, agentDisplayName(agent), agent.department, agent.title]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle)),
    );
  }, [agentsQuery.data, query]);

  useEffect(() => {
    if (filteredAgents.length === 0) {
      setSelectedAgentId(null);
      return;
    }
    if (!selectedAgentId || !filteredAgents.some((agent) => agent.id === selectedAgentId)) {
      const grouped = groupAgentsByTier(filteredAgents);
      const employeeGroups = groupEmployeesByDepartment(filteredAgents);
      setSelectedAgentId(
        employeeGroups[0]?.agents[0]?.id ??
          [...grouped.system].sort(compareAgentsByName)[0]?.id ??
          [...grouped.harness].sort(compareAgentsByName)[0]?.id ??
          [...grouped.workflow].sort(compareAgentsByName)[0]?.id ??
          null,
      );
    }
  }, [filteredAgents, selectedAgentId]);

  const selectedAgent = filteredAgents.find((agent) => agent.id === selectedAgentId) ?? null;
  const selectedTier = selectedAgent ? tierForAgent(selectedAgent) : "employee";

  // Workflow agents only expose the Overview tab; switching to one while on a
  // now-disabled tab would leave its empty content rendered under a disabled trigger.
  useEffect(() => {
    if (selectedTier === "workflow" && tab !== "overview") setTab("overview");
  }, [selectedTier, tab]);
  const imageEnabled = Boolean(selectedAgent && selectedTier !== "workflow");
  const image = useAgentImage(selectedAgent?.id, imageEnabled);
  const imageSnapshot = image.data;
  const editable = Boolean(selectedAgent && selectedTier !== "workflow" && imageSnapshot);
  const workforceEditable = Boolean(selectedAgent && selectedTier !== "workflow");

  const isConfigDirty = imageSnapshot
    ? configText !== JSON.stringify(imageSnapshot.image.config, null, 2) ||
      instructionsText !== (imageSnapshot.image.instructions ?? "")
    : false;

  useEffect(() => {
    if (!imageSnapshot) {
      setConfigText("");
      setInstructionsText("");
      return;
    }
    setConfigText(JSON.stringify(imageSnapshot.image.config, null, 2));
    setInstructionsText(imageSnapshot.image.instructions ?? "");
    setSaveError(null);
    setSaveNotice(null);
  }, [imageSnapshot]);

  async function doSave(body: AgentImageUpdate, label: string) {
    if (!selectedAgent) return;
    setSaveError(null);
    setSaveNotice(null);
    try {
      await updateImage.mutateAsync({
        agentId: selectedAgent.id,
        body,
        etag: imageSnapshot?.etag,
      });
      setSaveNotice(`${label} saved.`);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Save failed");
    }
  }

  function commitSave(body: AgentImageUpdate, label: string) {
    if (tierRequiresEditConfirmation(selectedTier)) {
      setPendingSave({ body, label, tier: selectedTier });
      return;
    }
    void doSave(body, label);
  }

  function saveConfig() {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(configText) as Record<string, unknown>;
    } catch {
      setSaveError("Config must be valid JSON.");
      return;
    }
    commitSave({ config: parsed, instructions: instructionsText }, "Agent image");
  }

  return {
    allowed,
    filteredAgents,
    selectedAgentId,
    setSelectedAgentId,
    query,
    setQuery,
    selectedAgent,
    selectedTier,
    editable,
    workforceEditable,
    tab,
    setTab,
    isConfigDirty,
    imageSnapshot,
    imageIsError: image.isError,
    imageError: image.error,
    configText,
    setConfigText,
    instructionsText,
    setInstructionsText,
    onSaveConfig: saveConfig,
    updateImagePending: updateImage.isPending,
    saveError,
    saveNotice,
    onRefetch: () => {
      void agentsQuery.refetch();
      void image.refetch();
    },
    commitSave,
    pendingSave,
    setPendingSave,
    onConfirmPendingSave: () => {
      if (!pendingSave) return;
      const next = pendingSave;
      setPendingSave(null);
      void doSave(next.body, next.label);
    },
  };
}