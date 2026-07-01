import { SaveIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { agentDisplayName } from "../../workforce-utils";
import type { PermissionsTabState } from "../usePermissionsTab";

export function PermissionsInstructionsTabView({
  agent,
  editable,
  scopeLabel,
  instructionDraft,
  setInstructionDraft,
  saveInstructions,
  updateInstructions,
  effective,
  agentInstructionDraft,
  setAgentInstructionDraft,
  saveAgentInstructions,
  updateAgentInstructions,
}: Pick<
  PermissionsTabState,
  | "agent"
  | "editable"
  | "scopeLabel"
  | "instructionDraft"
  | "setInstructionDraft"
  | "saveInstructions"
  | "updateInstructions"
  | "effective"
  | "agentInstructionDraft"
  | "setAgentInstructionDraft"
  | "saveAgentInstructions"
  | "updateAgentInstructions"
>) {
  return (
    <div className="space-y-4">
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
              <div className="p-4 text-sm text-muted-foreground">No inherited instructions.</div>
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
    </div>
  );
}