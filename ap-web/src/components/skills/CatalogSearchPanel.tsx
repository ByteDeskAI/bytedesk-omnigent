import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  useApplySkillPreview,
  useCreateSkillPreview,
  useSearchSkills,
  type SkillPreview,
  type SkillSearchResult,
} from "@/hooks/useSkills";
import { CatalogHitRow } from "./CatalogHitRow";

export function CatalogSearchPanel({ targetAgentIds }: { targetAgentIds: string[] }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SkillSearchResult[]>([]);
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const search = useSearchSkills();
  const createPreview = useCreateSkillPreview();
  const applyPreview = useApplySkillPreview();

  const stageHit = async (source: string, sourceRef: string) => {
    setError(null);
    try {
      const staged = await createPreview.mutateAsync({
        target_agent_ids: targetAgentIds,
        source,
        source_ref: sourceRef,
        install_mode: "skip_existing",
      });
      setPreview(staged);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    }
  };

  return (
    <div className="space-y-2">
      <div className="text-xs font-medium text-muted-foreground">Search catalog</div>
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
            .catch((err: unknown) => {
              setError(err instanceof Error ? err.message : "Search failed");
            });
        }}
      >
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="platform architect"
          aria-label="Search ByteDesk catalog"
        />
        <Button type="submit" size="sm" disabled={search.isPending || !query.trim()}>
          Search
        </Button>
      </form>
      {error && <div className="text-xs text-destructive">{error}</div>}
      <div className="space-y-1.5">
        {results.map((hit) => (
          <CatalogHitRow
            key={`${hit.source}:${hit.source_ref ?? hit.name}`}
            name={hit.name}
            description={hit.description ?? hit.source_ref}
            source={hit.source}
            sourceRef={hit.source_ref ?? ""}
            targetAgentIds={targetAgentIds}
            onStage={() => void stageHit(hit.source, hit.source_ref ?? "")}
            staging={createPreview.isPending}
          />
        ))}
      </div>
      {preview && (
        <div className="space-y-2 rounded-md border border-border bg-muted/20 p-2.5">
          <div className="text-xs font-medium">Preview ready</div>
          <div className="text-xs text-muted-foreground">
            {preview.skills.map((skill) => skill.name).join(", ")} →{" "}
            {preview.target_actions.filter((action) => action.action !== "skip").length} apply
          </div>
          <Button
            size="sm"
            disabled={applyPreview.isPending || targetAgentIds.length === 0}
            onClick={() => {
              void applyPreview
                .mutateAsync({ previewId: preview.id, targetAgentIds })
                .then(() => setPreview(null))
                .catch((err: unknown) => {
                  setError(err instanceof Error ? err.message : "Apply failed");
                });
            }}
          >
            Apply to scope
          </Button>
        </div>
      )}
    </div>
  );
}