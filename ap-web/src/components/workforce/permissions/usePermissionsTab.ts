import { useEffect, useMemo, useState } from "react";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useConnectorsCatalog } from "@/hooks/useConnectors";
import { useInstalledSkills, useSearchSkills, type SkillSearchResult } from "@/hooks/useSkills";
import {
  useUpdateWorkforceAgentInstructions,
  useUpdateWorkforceInstructions,
  useUpsertWorkforceAgentOverride,
  useUpsertWorkforceConnector,
  useUpsertWorkforceSkill,
  useUpsertWorkforceTool,
  useWorkforceAgentEffective,
  useWorkforceScope,
  useWorkforceScopes,
  useWorkforceToolCatalog,
} from "@/hooks/useWorkforce";
import type {
  WorkforceEffectiveConnector,
  WorkforceEffectiveTool,
  WorkforceScopeKind,
  WorkforceToolCatalogItem,
} from "@/lib/workforceApi";
import {
  compareText,
  sameConnectionSelection,
  scopeDisplayName,
  toolsForConnection,
  workforceScopeSlug,
} from "../workforce-utils";

export type PermissionsTabState = ReturnType<typeof usePermissionsTab>;

export function usePermissionsTab(agent: AvailableAgent, editable: boolean) {
  const department = agent.department?.trim() || null;
  const departmentScopeId = workforceScopeSlug(department);
  const [scopeKind, setScopeKind] = useState<WorkforceScopeKind>(
    departmentScopeId ? "department" : "organization",
  );
  const scopeId = scopeKind === "department" ? departmentScopeId : null;
  const scopes = useWorkforceScopes();
  const scope = useWorkforceScope(scopeKind, scopeId, editable);
  const effective = useWorkforceAgentEffective(agent.id, editable);
  const connectorCatalog = useConnectorsCatalog();
  const installedSkills = useInstalledSkills(agent.id);
  const toolCatalog = useWorkforceToolCatalog();
  const updateInstructions = useUpdateWorkforceInstructions();
  const updateAgentInstructions = useUpdateWorkforceAgentInstructions();
  const upsertConnector = useUpsertWorkforceConnector();
  const upsertSkill = useUpsertWorkforceSkill();
  const upsertTool = useUpsertWorkforceTool();
  const upsertOverride = useUpsertWorkforceAgentOverride();
  const skillSearch = useSearchSkills();
  const [instructionDraft, setInstructionDraft] = useState("");
  const [agentInstructionDraft, setAgentInstructionDraft] = useState("");
  const [selectedByConnection, setSelectedByConnection] = useState<Record<string, string[]>>({});
  const [skillQuery, setSkillQuery] = useState("");
  const [skillResults, setSkillResults] = useState<SkillSearchResult[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const rows = useMemo(
    () =>
      (connectorCatalog.data ?? []).flatMap((provider) =>
        provider.connections.map((connection) => ({
          provider,
          connection,
          tools: toolsForConnection(provider, connection),
        })),
      ),
    [connectorCatalog.data],
  );

  useEffect(() => {
    setScopeKind(departmentScopeId ? "department" : "organization");
  }, [agent.id, departmentScopeId]);

  useEffect(() => {
    setInstructionDraft(scope.data?.instruction?.body ?? "");
    setNotice(null);
    setError(null);
  }, [scope.data?.instruction?.body, scopeKind, scopeId]);

  useEffect(() => {
    const agentInstruction = (effective.data?.instructions ?? []).find(
      (item) => item.scopeKind === "agent",
    );
    setAgentInstructionDraft(agentInstruction?.body ?? "");
  }, [agent.id, effective.data?.instructions]);

  useEffect(() => {
    const next: Record<string, string[]> = {};
    for (const row of rows) {
      next[row.connection.id] = (scope.data?.connectors ?? [])
        .filter((item) => item.connectionId === row.connection.id && item.enabled)
        .map((item) => `${item.serviceKey}:${item.toolKey}`);
    }
    setSelectedByConnection((prev) => (sameConnectionSelection(prev, next) ? prev : next));
  }, [rows, scope.data?.connectors]);

  const scopeSummary = (scopes.data?.scopes ?? []).find(
    (item) => item.scopeKind === scopeKind && item.scopeId === (scopeId ?? "organization"),
  );
  const scopeLabel = scopeDisplayName(scopeKind, department);
  const effectiveSkills = [...(effective.data?.skills ?? [])].sort((a, b) =>
    compareText(a.skillName, b.skillName),
  );
  const effectiveConnectors = [...(effective.data?.connectors ?? [])].sort((a, b) =>
    compareText(a.itemKey, b.itemKey),
  );
  const scopeSkills = [...(scope.data?.skills ?? [])].sort((a, b) =>
    compareText(a.skillName, b.skillName),
  );
  const agentImageSkills = [...(installedSkills.data ?? [])].sort((a, b) =>
    compareText(a.name, b.name),
  );
  const scopeTools = [...(scope.data?.tools ?? [])].sort((a, b) =>
    compareText(a.toolKey, b.toolKey),
  );
  const effectiveTools = [...(effective.data?.tools ?? [])].sort((a, b) =>
    compareText(a.label, b.label),
  );
  const effectiveToolByKey = new Map(effectiveTools.map((item) => [item.toolKey, item]));
  const scopeToolByKey = new Map(scopeTools.map((item) => [item.toolKey, item]));
  const toolCatalogRows = [...(toolCatalog.data?.tools ?? [])].sort(
    (a, b) => compareText(a.group, b.group) || compareText(a.label, b.label),
  );

  function setTool(connectionId: string, token: string, checked: boolean) {
    setSelectedByConnection((prev) => {
      const current = new Set(prev[connectionId] ?? []);
      if (checked) current.add(token);
      else current.delete(token);
      return { ...prev, [connectionId]: Array.from(current) };
    });
  }

  function connectorLabel(item: WorkforceEffectiveConnector): string {
    const row = rows.find((candidate) => candidate.connection.id === item.connectionId);
    const token = `${item.serviceKey}:${item.toolKey}`;
    const tool = row?.tools.find((candidate) => candidate.token === token);
    if (!row || !tool) return item.itemKey;
    return `${row.connection.displayName} · ${tool.name}`;
  }

  async function saveInstructions() {
    setNotice(null);
    setError(null);
    try {
      await updateInstructions.mutateAsync({ scopeKind, scopeId, body: instructionDraft });
      setNotice(`${scopeLabel} instructions saved.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    }
  }

  async function saveAgentInstructions() {
    setNotice(null);
    setError(null);
    try {
      await updateAgentInstructions.mutateAsync({
        agentId: agent.id,
        body: agentInstructionDraft,
      });
      setNotice("Agent instructions saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    }
  }

  async function saveConnector(connectionId: string, tools: string[], enabled: boolean) {
    setNotice(null);
    setError(null);
    try {
      await upsertConnector.mutateAsync({
        scopeKind,
        scopeId,
        connectionId,
        tools,
        enabled,
        replace: true,
        reconcile: true,
        materialize: true,
      });
      setNotice(`${scopeLabel} connector actions saved.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Connector save failed");
    }
  }

  async function searchSkills() {
    setNotice(null);
    setError(null);
    try {
      const response = await skillSearch.mutateAsync({
        query: skillQuery,
        sources: ["github_marketplace"],
        limit: 8,
      });
      setSkillResults(response.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
    }
  }

  async function saveSkill(hit: SkillSearchResult, enabled = true) {
    if (!hit.source_ref) return;
    setNotice(null);
    setError(null);
    try {
      await upsertSkill.mutateAsync({
        scopeKind,
        scopeId,
        skillName: hit.name,
        source: hit.source,
        sourceRef: hit.source_ref,
        enabled,
        reconcile: true,
      });
      setNotice(`${scopeLabel} skill saved.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Skill save failed");
    }
  }

  async function toggleScopeSkill(skillName: string, enabled: boolean) {
    const current = scopeSkills.find((item) => item.skillName === skillName);
    if (!current) return;
    setNotice(null);
    setError(null);
    try {
      await upsertSkill.mutateAsync({
        scopeKind,
        scopeId,
        skillName,
        source: current.source,
        sourceRef: current.sourceRef,
        enabled,
        reconcile: true,
      });
      setNotice(`${scopeLabel} skill saved.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Skill save failed");
    }
  }

  async function setScopeTool(tool: WorkforceToolCatalogItem, enabled: boolean) {
    setNotice(null);
    setError(null);
    try {
      await upsertTool.mutateAsync({
        scopeKind,
        scopeId,
        toolKey: tool.toolKey,
        enabled,
        reconcile: true,
      });
      setNotice(`${scopeLabel} tool permission saved.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Tool permission save failed");
    }
  }

  async function toggleOverride(
    itemKind: "connector" | "skill" | "tool",
    itemKey: string,
    enabled: boolean,
  ) {
    setNotice(null);
    setError(null);
    try {
      await upsertOverride.mutateAsync({
        agentId: agent.id,
        itemKind,
        itemKey,
        enabled,
        reconcile: true,
      });
      setNotice("Agent override saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Override save failed");
    }
  }

  function toolStateLabel(tool: WorkforceToolCatalogItem): string {
    const assignment = scopeToolByKey.get(tool.toolKey);
    if (!assignment) return "Not set here";
    return assignment.enabled ? "Granted here" : "Denied here";
  }

  function inheritedToolLabel(tool: WorkforceEffectiveTool | undefined): string {
    if (!tool) return "Not managed — agent config applies";
    if (!tool.inherited) return "Agent override";
    const last = tool.inheritedFrom[tool.inheritedFrom.length - 1];
    if (!last) return "Inherited";
    return last.scopeKind === "organization" ? "Organization" : last.scopeId;
  }

  return {
    agent,
    editable,
    department,
    departmentScopeId,
    scopeKind,
    setScopeKind,
    scopeId,
    scopes,
    scope,
    effective,
    connectorCatalog,
    installedSkills,
    toolCatalog,
    updateInstructions,
    updateAgentInstructions,
    upsertConnector,
    upsertSkill,
    upsertTool,
    upsertOverride,
    skillSearch,
    instructionDraft,
    setInstructionDraft,
    agentInstructionDraft,
    setAgentInstructionDraft,
    selectedByConnection,
    skillQuery,
    setSkillQuery,
    skillResults,
    notice,
    error,
    rows,
    scopeSummary,
    scopeLabel,
    effectiveSkills,
    effectiveConnectors,
    scopeSkills,
    agentImageSkills,
    scopeTools,
    effectiveTools,
    effectiveToolByKey,
    scopeToolByKey,
    toolCatalogRows,
    setTool,
    connectorLabel,
    saveInstructions,
    saveAgentInstructions,
    saveConnector,
    searchSkills,
    saveSkill,
    toggleScopeSkill,
    setScopeTool,
    toggleOverride,
    toolStateLabel,
    inheritedToolLabel,
  };
}
