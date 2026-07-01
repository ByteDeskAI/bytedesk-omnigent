import { SearchIcon } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  useApplySkillPreview,
  useCreateSkillPreview,
  useInstalledSkills,
  useSearchSkills,
  type SkillPreview,
  type SkillSearchResult,
} from "@/hooks/useSkills";

export function SkillsTab({ agentId, editable }: { agentId: string; editable: boolean }) {
  const installed = useInstalledSkills(agentId);
  const search = useSearchSkills();
  const createPreview = useCreateSkillPreview();
  const applyPreview = useApplySkillPreview();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SkillSearchResult[]>([]);
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function stageInstall(hit: SkillSearchResult) {
    if (!hit.source_ref) return;
    setError(null);
    try {
      setPreview(
        await createPreview.mutateAsync({
          target_agent_ids: [agentId],
          source: hit.source,
          source_ref: hit.source_ref,
          install_mode: "skip_existing",
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    }
  }

  async function stageRemove(skillName: string) {
    setError(null);
    try {
      setPreview(
        await createPreview.mutateAsync({
          operation: "remove",
          target_agent_ids: [agentId],
          skill_names: [skillName],
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    }
  }

  async function applyStaged() {
    if (!preview) return;
    setError(null);
    try {
      await applyPreview.mutateAsync({ previewId: preview.id, targetAgentIds: [agentId] });
      setPreview(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Apply failed");
    }
  }

  return (
    <div className="mc-fade-up grid gap-4 p-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <section className="mc-surface">
        <div className="mc-label border-b border-border-dimmer px-3 py-2">Catalog</div>
        <div className="space-y-3 p-3">
          <form
            className="flex gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              setError(null);
              void search
                .mutateAsync({ query, sources: ["github_marketplace"], limit: 8 })
                .then((response) => {
                  setResults(response.data);
                  setPreview(null);
                })
                .catch((err: unknown) =>
                  setError(err instanceof Error ? err.message : "Search failed"),
                );
            }}
          >
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search skills"
              aria-label="Search skills"
            />
            <Button type="submit" disabled={!editable || !query.trim() || search.isPending}>
              <SearchIcon /> Search
            </Button>
          </form>
          <div className="grid gap-2 md:grid-cols-2">
            {results.map((hit) => (
              <button
                key={`${hit.source}:${hit.source_ref ?? hit.name}`}
                type="button"
                className="rounded-md border border-border px-3 py-2 text-left hover:bg-muted/40 disabled:opacity-50"
                disabled={!editable || !hit.source_ref || createPreview.isPending}
                onClick={() => void stageInstall(hit)}
              >
                <div className="truncate text-sm font-medium">{hit.name}</div>
                <div className="line-clamp-2 text-xs text-muted-foreground">
                  {hit.description || hit.source_ref}
                </div>
              </button>
            ))}
          </div>
          {error && <div className="text-sm text-destructive">{error}</div>}
          {preview && (
            <div className="mc-surface bg-bg-elevated p-3">
              <div className="text-sm font-medium">Preview ready</div>
              <div className="mt-1 text-xs text-muted-foreground">
                {preview.operation} {preview.skill_names.join(", ")}
              </div>
              <Button
                className="mt-3"
                size="sm"
                disabled={!editable || applyPreview.isPending}
                onClick={() => void applyStaged()}
              >
                Apply preview
              </Button>
            </div>
          )}
        </div>
      </section>

      <section className="mc-surface">
        <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
          <div className="mc-label">Installed</div>
          <Badge variant="secondary">{installed.data?.length ?? 0}</Badge>
        </div>
        <div className="max-h-[42rem] divide-y divide-border-dimmer overflow-y-auto">
          {(installed.data ?? []).map((skill) => (
            <div key={skill.name} className="p-3">
              <div className="truncate text-sm font-medium">{skill.name}</div>
              <div className="line-clamp-2 text-xs text-muted-foreground">{skill.description}</div>
              <Button
                className="mt-2"
                size="xs"
                variant="outline"
                disabled={!editable || createPreview.isPending}
                onClick={() => void stageRemove(skill.name)}
              >
                Remove
              </Button>
            </div>
          ))}
          {!installed.isLoading && (installed.data ?? []).length === 0 && (
            <div className="p-4 text-sm text-muted-foreground">No installed skills.</div>
          )}
        </div>
      </section>
    </div>
  );
}