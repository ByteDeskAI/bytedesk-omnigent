import {
  ChevronRightIcon,
  FileTextIcon,
  PlugIcon,
  PuzzleIcon,
  SaveIcon,
  SearchIcon,
  TerminalIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { agentDisplayName, inheritedSourceLabel } from "../workforce-utils";
import type { usePermissionsTab } from "./usePermissionsTab";

export function PermissionsTabView(s: ReturnType<typeof usePermissionsTab>) {
  const {
    agent, editable, department, departmentScopeId, scopeKind, setScopeKind,
    scopes, scope, effective, connectorCatalog, installedSkills, toolCatalog,
    updateInstructions, updateAgentInstructions, upsertConnector, upsertSkill, upsertTool,
    upsertOverride, skillSearch, instructionDraft, setInstructionDraft, agentInstructionDraft,
    setAgentInstructionDraft, selectedByConnection, skillQuery, setSkillQuery, skillResults,
    notice, error, rows, scopeSummary, scopeLabel, effectiveSkills, effectiveConnectors,
    scopeSkills, agentImageSkills, scopeTools, effectiveTools, effectiveToolByKey, scopeToolByKey,
    toolCatalogRows, setTool, connectorLabel, saveInstructions, saveAgentInstructions,
    saveConnector, searchSkills, saveSkill, toggleScopeSkill, setScopeTool, toggleOverride,
    toolStateLabel, inheritedToolLabel,
  } = s;
  return (
    <div className="mc-fade-up space-y-4 p-4">
      <section className="mc-surface flex flex-wrap items-center justify-between gap-3 p-3">
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setScopeKind("organization")}
            disabled={!editable}
            className={cn(
              "mc-label rounded-full px-2.5 py-1.5 transition-colors disabled:opacity-50",
              scopeKind === "organization"
                ? "bg-accent-orange/15 text-accent-orange"
                : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
            )}
          >
            Organization
          </button>
          {departmentScopeId && (
            <>
              <ChevronRightIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
              <button
                type="button"
                onClick={() => setScopeKind("department")}
                disabled={!editable}
                className={cn(
                  "mc-label rounded-full px-2.5 py-1.5 transition-colors disabled:opacity-50",
                  scopeKind === "department"
                    ? "bg-accent-orange/15 text-accent-orange"
                    : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                )}
              >
                {department}
              </button>
            </>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="mc-value">
            {scopeSummary?.agentIds.length ?? 0} agents
          </Badge>
          <Badge variant="outline" className="mc-value">
            rev {scope.data?.revision ?? scopes.data?.revision ?? "-"}
          </Badge>
        </div>
      </section>

      <Tabs defaultValue="tools" className="space-y-4">
        <TabsList variant="line" className="flex-wrap" aria-label="Permission groups">
          <TabsTrigger value="tools">
            <TerminalIcon /> Tools
          </TabsTrigger>
          <TabsTrigger value="instructions">
            <FileTextIcon /> Instructions
          </TabsTrigger>
          <TabsTrigger value="skills">
            <PuzzleIcon /> Skills
          </TabsTrigger>
          <TabsTrigger value="connectors">
            <PlugIcon /> Connectors
          </TabsTrigger>
        </TabsList>

        <TabsContent value="tools" className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_25rem]">
            <section className="mc-surface">
              <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
                <div>
                  <div className="mc-label">{scopeLabel} Builtin Tools</div>
                  <div className="text-xs text-muted-foreground">
                    Explicit grant or deny rows for this inheritance level.
                  </div>
                </div>
                <Badge variant="secondary">{scopeTools.length}</Badge>
              </div>
              <div className="grid max-h-[32rem] gap-2 overflow-y-auto p-3 md:grid-cols-2">
                {toolCatalogRows.map((tool) => {
                  const assignment = scopeToolByKey.get(tool.toolKey);
                  return (
                    <div
                      key={tool.toolKey}
                      data-testid={`scope-tool-row-${tool.toolKey}`}
                      className="flex min-w-0 flex-col gap-2 rounded-md border border-border/70 px-3 py-2 text-sm"
                    >
                      <div className="min-w-0">
                        <div className="flex min-w-0 items-center justify-between gap-2">
                          <span className="truncate font-medium">{tool.label}</span>
                          <Badge variant={assignment?.enabled ? "default" : "outline"}>
                            {toolStateLabel(tool)}
                          </Badge>
                        </div>
                        <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                          {tool.toolKey} · {tool.description}
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          variant={assignment?.enabled ? "secondary" : "outline"}
                          disabled={!editable || upsertTool.isPending}
                          onClick={() => void setScopeTool(tool, true)}
                        >
                          Grant here
                        </Button>
                        <Button
                          size="sm"
                          variant={assignment && !assignment.enabled ? "secondary" : "outline"}
                          disabled={!editable || upsertTool.isPending}
                          onClick={() => void setScopeTool(tool, false)}
                        >
                          Deny here
                        </Button>
                      </div>
                    </div>
                  );
                })}
                {!toolCatalog.isLoading && toolCatalogRows.length === 0 && (
                  <div className="text-sm text-muted-foreground">No builtin tools reported.</div>
                )}
              </div>
            </section>

            <section className="mc-surface">
              <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
                <div>
                  <div className="mc-label text-accent-cyan">Effective Agent Tools</div>
                  <div className="text-xs text-muted-foreground">{agentDisplayName(agent)}</div>
                </div>
                <Badge variant="secondary">
                  {effectiveTools.filter((item) => item.enabled).length}
                </Badge>
              </div>
              <div className="max-h-[32rem] divide-y divide-border-dimmer overflow-y-auto">
                {toolCatalogRows.map((catalogItem) => {
                  const tool = effectiveToolByKey.get(catalogItem.toolKey);
                  const enabled = tool?.enabled ?? false;
                  return (
                    <div
                      key={catalogItem.toolKey}
                      data-testid={`effective-tool-row-${catalogItem.toolKey}`}
                      className="p-3"
                    >
                      <div className="flex min-w-0 items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium">{catalogItem.label}</div>
                          <div className="mt-1 text-xs text-muted-foreground">
                            {catalogItem.toolKey} · {inheritedToolLabel(tool)}
                          </div>
                        </div>
                        <Badge variant={enabled ? "default" : "outline"}>
                          {enabled ? "Enabled" : "Disabled"}
                        </Badge>
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        className="mt-2"
                        disabled={!editable || upsertOverride.isPending}
                        onClick={() => void toggleOverride("tool", catalogItem.toolKey, !enabled)}
                      >
                        {enabled ? "Disable for agent" : "Enable for agent"}
                      </Button>
                    </div>
                  );
                })}
                {!toolCatalog.isLoading && toolCatalogRows.length === 0 && (
                  <div className="p-4 text-sm text-muted-foreground">
                    No builtin tools reported.
                  </div>
                )}
              </div>
            </section>
          </div>
        </TabsContent>

        <TabsContent value="instructions" className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_23rem]">
            <section className="mc-surface">
              <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
                <div className="mc-label">{scopeLabel} Instructions</div>
                <Button
                  size="sm"
                  disabled={!editable || updateInstructions.isPending}
                  onClick={() => void saveInstructions()}
                >
                  <SaveIcon /> Save
                </Button>
              </div>
              <Textarea
                className="min-h-52 resize-y rounded-none border-0 font-mono text-xs focus-visible:ring-0"
                value={instructionDraft}
                onChange={(event) => setInstructionDraft(event.target.value)}
                disabled={!editable}
                aria-label={`${scopeLabel} instructions`}
              />
            </section>

            <section className="mc-surface">
              <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
                <div className="mc-label text-accent-cyan">Inherited Instructions</div>
                <Badge variant="secondary">{effective.data?.instructions?.length ?? 0}</Badge>
              </div>
              <div className="max-h-64 divide-y divide-border-dimmer overflow-y-auto">
                {(effective.data?.instructions ?? []).map((item) => (
                  <div key={item.id} className="p-3">
                    <div className="text-xs font-medium text-muted-foreground">
                      {item.scopeKind === "organization" ? "Organization" : item.scopeId}
                    </div>
                    <div className="mt-1 line-clamp-3 text-xs">{item.body}</div>
                  </div>
                ))}
                {!effective.isLoading && (effective.data?.instructions ?? []).length === 0 && (
                  <div className="p-4 text-sm text-muted-foreground">
                    No inherited instructions.
                  </div>
                )}
              </div>
            </section>
          </div>

          <section className="mc-surface">
            <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
              <div>
                <div className="mc-label">Agent Instructions</div>
                <div className="text-xs text-muted-foreground">{agentDisplayName(agent)}</div>
              </div>
              <Button
                size="sm"
                disabled={!editable || updateAgentInstructions.isPending}
                onClick={() => void saveAgentInstructions()}
              >
                <SaveIcon /> Save
              </Button>
            </div>
            <Textarea
              className="min-h-40 resize-y rounded-none border-0 font-mono text-xs focus-visible:ring-0"
              value={agentInstructionDraft}
              onChange={(event) => setAgentInstructionDraft(event.target.value)}
              disabled={!editable}
              aria-label="Agent instructions"
            />
          </section>
        </TabsContent>

        <TabsContent value="skills" className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_23rem]">
            <section className="mc-surface">
              <div className="mc-label border-b border-border-dimmer px-3 py-2">
                {scopeLabel} Skills
              </div>
              <div className="space-y-3 p-3">
                <form
                  className="flex gap-2"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void searchSkills();
                  }}
                >
                  <Input
                    value={skillQuery}
                    onChange={(event) => setSkillQuery(event.target.value)}
                    placeholder="Search skills"
                    aria-label="Search inherited skills"
                  />
                  <Button
                    type="submit"
                    disabled={!editable || !skillQuery.trim() || skillSearch.isPending}
                  >
                    <SearchIcon /> Search
                  </Button>
                </form>
                <div className="grid gap-2 md:grid-cols-2">
                  {skillResults.map((hit) => (
                    <button
                      key={`${hit.source}:${hit.source_ref ?? hit.name}`}
                      type="button"
                      className="rounded-md border border-border px-3 py-2 text-left hover:bg-muted/40 disabled:opacity-50"
                      disabled={!editable || !hit.source_ref || upsertSkill.isPending}
                      onClick={() => void saveSkill(hit, true)}
                    >
                      <div className="truncate text-sm font-medium">{hit.name}</div>
                      <div className="line-clamp-2 text-xs text-muted-foreground">
                        {hit.description || hit.source_ref}
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            </section>

            <section className="mc-surface">
              <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
                <div className="mc-label">Scope Skills</div>
                <Badge variant="secondary">
                  {scopeSkills.filter((item) => item.enabled).length}
                </Badge>
              </div>
              <div className="max-h-80 divide-y divide-border-dimmer overflow-y-auto">
                {scopeSkills.map((item) => (
                  <div key={item.skillName} className="p-3">
                    <div className="truncate text-sm font-medium">{item.skillName}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {item.sourceRef || item.source}
                    </div>
                    <Button
                      className="mt-2"
                      size="xs"
                      variant="outline"
                      disabled={!editable || upsertSkill.isPending}
                      onClick={() => void toggleScopeSkill(item.skillName, !item.enabled)}
                    >
                      {item.enabled ? "Disable" : "Enable"}
                    </Button>
                  </div>
                ))}
                {!scope.isLoading && scopeSkills.length === 0 && (
                  <div className="p-4 text-sm text-muted-foreground">No scope skills.</div>
                )}
              </div>
            </section>
          </div>

          <section className="mc-surface">
            <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
              <div className="mc-label text-accent-cyan">Effective Skills</div>
              <Badge variant="secondary">
                {effectiveSkills.filter((item) => item.enabled).length}
              </Badge>
            </div>
            <div className="max-h-80 divide-y divide-border-dimmer overflow-y-auto">
              {effectiveSkills.map((item) => (
                <div key={item.itemKey} className="flex items-start justify-between gap-3 p-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{item.skillName}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {inheritedSourceLabel(item)}
                    </div>
                  </div>
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={!editable || upsertOverride.isPending}
                    onClick={() => void toggleOverride("skill", item.itemKey, !item.enabled)}
                  >
                    {item.enabled ? "Disable for agent" : "Enable for agent"}
                  </Button>
                </div>
              ))}
              {!effective.isLoading && effectiveSkills.length === 0 && (
                <div className="p-4 text-sm text-muted-foreground">No inherited skills.</div>
              )}
            </div>
          </section>

          <section className="mc-surface">
            <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
              <div>
                <div className="mc-label text-accent-cyan">Agent Image Skills</div>
                <div className="text-xs text-muted-foreground">{agentDisplayName(agent)}</div>
              </div>
              <Badge variant="secondary">{agentImageSkills.length}</Badge>
            </div>
            <div className="max-h-80 divide-y divide-border-dimmer overflow-y-auto">
              {agentImageSkills.map((skill) => (
                <div key={skill.name} className="p-3">
                  <div className="truncate text-sm font-medium">{skill.name}</div>
                  <div className="line-clamp-2 text-xs text-muted-foreground">
                    {skill.description}
                  </div>
                </div>
              ))}
              {!installedSkills.isLoading && agentImageSkills.length === 0 && (
                <div className="p-4 text-sm text-muted-foreground">
                  No agent image skills installed.
                </div>
              )}
            </div>
          </section>
        </TabsContent>

        <TabsContent value="connectors" className="space-y-4">
          <section className="mc-surface">
            <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
              <div>
                <div className="mc-label">{scopeLabel} Connector Actions</div>
                <div className="text-xs text-muted-foreground">
                  {(scope.data?.connectors ?? []).filter((item) => item.enabled).length} active
                </div>
              </div>
            </div>
            <div className="space-y-3 p-3">
              {rows.map(({ provider, connection, tools }) => {
                const selected = selectedByConnection[connection.id] ?? [];
                return (
                  <div key={connection.id} className="rounded-md border border-border-dimmer">
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-dimmer px-3 py-2">
                      <div>
                        <div className="text-sm font-medium">{connection.displayName}</div>
                        <div className="text-xs text-muted-foreground">
                          {provider.name} · {selected.length}/{tools.length} permissions
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={!editable || tools.length === 0 || upsertConnector.isPending}
                          onClick={() => void saveConnector(connection.id, [], false)}
                        >
                          Disable all
                        </Button>
                        <Button
                          size="sm"
                          disabled={
                            !editable || selected.length === 0 || upsertConnector.isPending
                          }
                          onClick={() => void saveConnector(connection.id, selected, true)}
                        >
                          <PlugIcon /> Save permissions
                        </Button>
                      </div>
                    </div>
                    <div className="grid gap-2 p-3 md:grid-cols-2 xl:grid-cols-3">
                      {tools.map((tool) => (
                        <label
                          key={tool.token}
                          className="flex items-start gap-2 rounded-md border border-border/70 px-3 py-2 text-sm"
                        >
                          <input
                            type="checkbox"
                            className="mt-1 size-3.5"
                            checked={selected.includes(tool.token)}
                            disabled={!editable}
                            onChange={(event) =>
                              setTool(connection.id, tool.token, event.target.checked)
                            }
                            aria-label={`${scopeLabel} ${connection.displayName} ${tool.name}`}
                          />
                          <span className="min-w-0">
                            <span className="block truncate font-medium">{tool.name}</span>
                            <span className="block truncate text-xs text-muted-foreground">
                              {tool.serviceName} · {tool.mcpTool}
                            </span>
                          </span>
                        </label>
                      ))}
                      {tools.length === 0 && (
                        <div className="text-sm text-muted-foreground">No enabled actions.</div>
                      )}
                    </div>
                  </div>
                );
              })}
              {!connectorCatalog.isLoading && rows.length === 0 && (
                <div className="text-sm text-muted-foreground">No connector connections.</div>
              )}
            </div>
          </section>

          <section className="mc-surface">
            <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
              <div className="mc-label text-accent-cyan">Effective Connector Permissions</div>
              <Badge variant="secondary">
                {effectiveConnectors.filter((item) => item.enabled).length}
              </Badge>
            </div>
            <div className="max-h-80 divide-y divide-border-dimmer overflow-y-auto">
              {effectiveConnectors.map((item) => (
                <div key={item.itemKey} className="flex items-start justify-between gap-3 p-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{connectorLabel(item)}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {inheritedSourceLabel(item)}
                    </div>
                  </div>
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={!editable || upsertOverride.isPending}
                    onClick={() => void toggleOverride("connector", item.itemKey, !item.enabled)}
                  >
                    {item.enabled ? "Disable for agent" : "Enable for agent"}
                  </Button>
                </div>
              ))}
              {!effective.isLoading && effectiveConnectors.length === 0 && (
                <div className="p-4 text-sm text-muted-foreground">
                  No inherited connector permissions.
                </div>
              )}
            </div>
          </section>
        </TabsContent>
      </Tabs>

      <div className="min-h-5 text-sm">
        {error && <span className="text-destructive">{error}</span>}
        {!error && notice && <span className="text-muted-foreground">{notice}</span>}
      </div>
    </div>
  );
}
