import { useCreateSkillPreview } from "@/hooks/useSkills";

export function CatalogHitRow({
  name,
  description,
  source,
  sourceRef,
  targetAgentIds,
  onStage,
  staging = false,
}: {
  name: string;
  description: string | null;
  source: string;
  sourceRef: string;
  targetAgentIds: string[];
  onStage?: () => void;
  staging?: boolean;
}) {
  const createPreview = useCreateSkillPreview();
  const stage =
    onStage ??
    (() => {
      void createPreview.mutateAsync({
        target_agent_ids: targetAgentIds,
        source,
        source_ref: sourceRef,
        install_mode: "skip_existing",
      });
    });

  return (
    <button
      type="button"
      className="w-full rounded-md border border-border px-2.5 py-2 text-left hover:bg-muted/40"
      disabled={staging || createPreview.isPending || !sourceRef}
      onClick={stage}
    >
      <div className="text-sm font-medium">{name}</div>
      <div className="line-clamp-2 text-xs text-muted-foreground">{description}</div>
    </button>
  );
}