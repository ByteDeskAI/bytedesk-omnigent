import { useEffect, useMemo, useRef, useState } from "react";
import {
  departmentId,
  findConcierge,
  homeWorkspaceFromEntries,
  scopeLabel,
  scopeMatchesAgent,
  type DepartmentGroup,
  type SkillScope,
} from "@/components/skills/skills-utils";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useHosts } from "@/hooks/useHosts";
import {
  useInstalledSkills,
  useSkillMarketplaces,
  useSkillRecommendations,
  useStartSkillsConciergeSession,
} from "@/hooks/useSkills";
import { tierForAgent } from "@/lib/agentTiers";
import { bindOnlyOnlineRunner, launchRunner } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import type { SkillsPageShellProps } from "./organisms/SkillsPageShell";

export function useSkillsPage(): SkillsPageShellProps {
  const agentsQuery = useAvailableAgents();
  const installed = useInstalledSkills();
  const marketplaces = useSkillMarketplaces();
  const { mutateAsync: startConciergeSession } = useStartSkillsConciergeSession();
  const agents = useMemo(() => agentsQuery.data ?? [], [agentsQuery.data]);
  const hosts = useHosts();
  const onlineHost = useMemo(
    () => (hosts.data ?? []).find((host) => host.status === "online") ?? null,
    [hosts.data],
  );
  const homeListing = useHostFilesystem(onlineHost?.host_id ?? null, onlineHost ? "" : null);
  const launchWorkspace = useMemo(
    () => homeWorkspaceFromEntries(homeListing.data?.entries ?? []),
    [homeListing.data],
  );

  const [selectedScope, setSelectedScope] = useState<SkillScope>({
    kind: "organization",
    id: "omnigent",
  });
  const hasDefaultedScope = useRef(false);

  const agentRows = useMemo(
    () =>
      agents
        .filter(
          (agent) => tierForAgent(agent) !== "workflow" && Boolean(agent.department || agent.title),
        )
        .sort(
          (a, b) =>
            departmentId(a).localeCompare(departmentId(b), undefined, { sensitivity: "base" }) ||
            a.display_name.localeCompare(b.display_name, undefined, { sensitivity: "base" }),
        ),
    [agents],
  );

  const departmentGroups = useMemo<DepartmentGroup[]>(() => {
    const groups = new Map<string, typeof agentRows>();
    for (const agent of agentRows) {
      const department = departmentId(agent);
      groups.set(department, [...(groups.get(department) ?? []), agent]);
    }
    return [...groups.entries()]
      .map(([id, departmentAgents]) => ({ id, agents: departmentAgents }))
      .sort((a, b) => a.id.localeCompare(b.id, undefined, { sensitivity: "base" }));
  }, [agentRows]);

  const targetAgentIds = useMemo(
    () =>
      agentRows.filter((agent) => scopeMatchesAgent(selectedScope, agent)).map((agent) => agent.id),
    [agentRows, selectedScope],
  );

  useEffect(() => {
    if (!hasDefaultedScope.current && agentRows.length > 0) {
      setSelectedScope({ kind: "organization", id: "omnigent" });
      hasDefaultedScope.current = true;
      return;
    }
    if (
      agentRows.length > 0 &&
      selectedScope.kind !== "organization" &&
      !agentRows.some((agent) => scopeMatchesAgent(selectedScope, agent))
    ) {
      setSelectedScope({ kind: "organization", id: "omnigent" });
    }
  }, [agentRows, selectedScope]);

  const selectedScopeLabel = scopeLabel(selectedScope, agentRows);
  const concierge = useMemo(() => findConcierge(agents), [agents]);
  const scopeSessionKey = `${selectedScope.kind}:${selectedScope.id}`;

  useEffect(() => {
    if (!concierge || targetAgentIds.length === 0) return;
    let cancelled = false;
    void (async () => {
      try {
        const session = await startConciergeSession({
          target_kind: selectedScope.kind,
          target_id: selectedScope.id,
          target_label: selectedScopeLabel,
          target_agent_ids: targetAgentIds,
        });
        if (!cancelled) {
          await useChatStore.getState().switchTo(session.session_id);
          const bound = await bindOnlyOnlineRunner(session.session_id);
          if (!bound && onlineHost && launchWorkspace) {
            await launchRunner(onlineHost.host_id, session.session_id, launchWorkspace);
          }
        }
      } catch {
        // Scope seed is best-effort; inline chat still works without it.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    concierge,
    scopeSessionKey,
    selectedScope.kind,
    selectedScope.id,
    selectedScopeLabel,
    startConciergeSession,
    targetAgentIds,
    onlineHost,
    launchWorkspace,
  ]);

  useEffect(() => {
    const previousConversationId = useChatStore.getState().conversationId;
    void useChatStore.getState().switchTo(null);
    return () => {
      void useChatStore.getState().switchTo(previousConversationId);
    };
  }, []);

  const scopeContextAgent = useMemo(() => {
    if (selectedScope.kind === "employee") {
      return agentRows.find((agent) => agent.id === selectedScope.id) ?? null;
    }
    if (selectedScope.kind === "department") {
      return agentRows.find((agent) => departmentId(agent) === selectedScope.id) ?? null;
    }
    return agentRows[0] ?? null;
  }, [agentRows, selectedScope]);
  const recommendations = useSkillRecommendations(
    scopeContextAgent?.department ?? null,
    scopeContextAgent?.title ?? null,
  );

  const scopedInstalled = useMemo(
    () =>
      (installed.data ?? [])
        .map((skill) => ({
          ...skill,
          agents: skill.agents.filter((agent) => targetAgentIds.includes(agent.id)),
        }))
        .filter((skill) => skill.agents.length > 0),
    [installed.data, targetAgentIds],
  );

  return {
    selectedScopeLabel,
    selectedScope,
    setSelectedScope,
    agentRows,
    departmentGroups,
    targetAgentIds,
    concierge: concierge ?? null,
    agents,
    agentsLoading: agentsQuery.isLoading,
    marketplaces: marketplaces.data ?? [],
    recommendations: recommendations.data ?? [],
    recommendationsLoading: recommendations.isLoading,
    scopedInstalled,
    installedLoading: installed.isLoading,
  };
}