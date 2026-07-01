import { FileTextIcon, FolderIcon, SaveIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useAgentImageTree, useReadAgentImageFile } from "@/hooks/useAgentImages";
import type { AgentImageUpdate } from "@/lib/agentImagesApi";
import { parentPath } from "./workforce-utils";

export function FilesTab({
  agentId,
  editable,
  etag,
  commitSave,
  busy,
}: {
  agentId: string;
  editable: boolean;
  etag: string | null | undefined;
  commitSave: (body: AgentImageUpdate, label: string) => void;
  busy: boolean;
}) {
  const [directory, setDirectory] = useState("");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const tree = useAgentImageTree(agentId, directory, editable);
  const readFile = useReadAgentImageFile();

  useEffect(() => {
    setDirectory("");
    setSelectedPath(null);
    setContent("");
  }, [agentId]);

  async function openFile(path: string) {
    const file = await readFile.mutateAsync({ agentId, path });
    setSelectedPath(file.path);
    setContent(file.content);
  }

  return (
    <div className="mc-fade-up grid min-h-0 gap-4 p-4 xl:grid-cols-[20rem_minmax(0,1fr)]">
      <section className="mc-surface min-h-[34rem]">
        <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
          <div className="mc-value truncate text-xs">{tree.data?.path || "."}</div>
          <Button
            variant="ghost"
            size="xs"
            disabled={!directory}
            onClick={() => setDirectory(parentPath(directory))}
          >
            Up
          </Button>
        </div>
        <div className="max-h-[42rem] overflow-y-auto p-2">
          {(tree.data?.entries ?? []).map((entry) => (
            <button
              key={entry.path}
              type="button"
              className="mb-1 flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted/50"
              disabled={!editable}
              onClick={() =>
                entry.type === "directory" ? setDirectory(entry.path) : void openFile(entry.path)
              }
            >
              {entry.type === "directory" ? (
                <FolderIcon className="size-4 shrink-0 text-accent-amber" />
              ) : (
                <FileTextIcon className="size-4 shrink-0 text-muted-foreground" />
              )}
              <span className="min-w-0 flex-1 truncate">{entry.name}</span>
              {entry.type === "file" && <span className="mc-value text-2xs">{entry.size}</span>}
            </button>
          ))}
          {tree.isError && (
            <div className="p-2 text-sm text-destructive">
              {tree.error instanceof Error ? tree.error.message : "File tree unavailable"}
            </div>
          )}
        </div>
      </section>
      <section className="mc-surface flex min-h-[34rem] flex-col">
        <div className="flex items-center justify-between gap-2 border-b border-border-dimmer px-3 py-2">
          <div className="mc-value min-w-0 truncate text-xs">{selectedPath || "Select file"}</div>
          <div className="flex gap-2">
            <Button
              size="xs"
              variant="outline"
              disabled={!editable || !selectedPath || selectedPath === "config.yaml" || busy}
              onClick={() =>
                selectedPath && commitSave({ remove: [selectedPath] }, `Remove ${selectedPath}`)
              }
            >
              Remove
            </Button>
            <Button
              size="xs"
              disabled={!editable || !selectedPath || busy}
              onClick={() =>
                selectedPath &&
                commitSave({ files: { [selectedPath]: content } }, `Save ${selectedPath}`)
              }
            >
              <SaveIcon /> Save file
            </Button>
          </div>
        </div>
        <Textarea
          className="min-h-0 flex-1 resize-none rounded-none border-0 font-mono text-xs focus-visible:ring-0"
          value={content}
          onChange={(event) => setContent(event.target.value)}
          disabled={!editable || !selectedPath}
          aria-label="Agent image file"
        />
        {etag === null && (
          <div className="border-t border-border px-3 py-2 text-xs text-muted-foreground">
            Save uses the latest loaded agent image version.
          </div>
        )}
      </section>
    </div>
  );
}