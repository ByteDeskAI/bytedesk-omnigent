import { SearchIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { agentDisplayName, inheritedSourceLabel } from "../../workforce-utils";
import type { PermissionsTabState } from "../usePermissionsTab";

export function PermissionsSkillsTabView({
  agent,
  editable,
  scopeLabel,
  skillQuery,
  setSkillQuery,
  searchSkills,
  skillSearch,
  skillResults,
  saveSkill,
  upsertSkill,
  scopeSkills,
  scope,
  toggleScopeSkill,
  effectiveSkills,
  effective,
  toggleOverride,
  upsertOverride,
  agentImageSkills,
  installedSkills,
}: Pick<
  PermissionsTabState,
  | "agent"
  | "editable"
  | "scopeLabel"
  | "skillQuery"
  | "setSkillQuery"
  | "searchSkills"
  | "skillSearch"
  | "skillResults"
  | "saveSkill"
  | "upsertSkill"
  | "scopeSkills"
  | "scope"
  | "toggleScopeSkill"
  | "effectiveSkills"
  | "effective"
  | "toggleOverride"
  | "upsertOverride"
  | "agentImageSkills"
  | "installedSkills"
>) {
  return (
    <div className="space-y-4">
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
            <Badge variant="secondary">{scopeSkills.filter((item) => item.enabled).length}</Badge>
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
              <div className="line-clamp-2 text-xs text-muted-foreground">{skill.description}</div>
            </div>
          ))}
          {!installedSkills.isLoading && agentImageSkills.length === 0 && (
            <div className="p-4 text-sm text-muted-foreground">No agent image skills installed.</div>
          )}
        </div>
      </section>
    </div>
  );
}