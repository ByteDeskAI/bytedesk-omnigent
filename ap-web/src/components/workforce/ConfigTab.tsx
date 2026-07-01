import { SaveIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

export function ConfigTab({
  editable,
  configText,
  setConfigText,
  instructionsText,
  setInstructionsText,
  onSave,
  busy,
  error,
  notice,
  dirty,
}: {
  editable: boolean;
  configText: string;
  setConfigText: (value: string) => void;
  instructionsText: string;
  setInstructionsText: (value: string) => void;
  onSave: () => void;
  busy: boolean;
  error: string | null;
  notice: string | null;
  dirty: boolean;
}) {
  return (
    <div className="mc-fade-up grid min-h-0 gap-4 p-4 xl:grid-cols-2">
      <section className="mc-surface flex min-h-[34rem] flex-col">
        <div className="mc-label border-b border-border-dimmer px-3 py-2">Instructions</div>
        <Textarea
          className="min-h-0 flex-1 resize-none rounded-none border-0 font-mono text-xs focus-visible:ring-0"
          value={instructionsText}
          onChange={(event) => setInstructionsText(event.target.value)}
          disabled={!editable}
          aria-label="Agent instructions"
        />
      </section>
      <section className="mc-surface flex min-h-[34rem] flex-col">
        <div className="mc-label border-b border-border-dimmer px-3 py-2">Config JSON</div>
        <Textarea
          className="min-h-0 flex-1 resize-none rounded-none border-0 font-mono text-xs focus-visible:ring-0"
          value={configText}
          onChange={(event) => setConfigText(event.target.value)}
          disabled={!editable}
          aria-label="Agent config"
        />
      </section>
      <div className="xl:col-span-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-h-5 items-center gap-2 text-sm">
          {error && <span className="text-destructive">{error}</span>}
          {!error && notice && <span className="text-muted-foreground">{notice}</span>}
          {!error && !notice && dirty && editable && (
            <Badge variant="outline" className="text-accent-amber">
              Unsaved changes
            </Badge>
          )}
        </div>
        <Button onClick={onSave} disabled={!editable || busy}>
          <SaveIcon /> Save image
        </Button>
      </div>
    </div>
  );
}