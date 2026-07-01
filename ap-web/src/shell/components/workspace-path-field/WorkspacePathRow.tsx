import { FolderOpenIcon } from "lucide-react";

export function WorkspacePathRow({
  path,
  active,
  onSelect,
  testId,
}: {
  path: string;
  active: boolean;
  onSelect: () => void;
  testId: string;
}) {
  return (
    <button
      type="button"
      id={testId}
      role="option"
      aria-selected={active}
      data-active={active}
      onMouseDown={(e) => {
        e.preventDefault();
        onSelect();
      }}
      className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition ${
        active ? "bg-accent text-accent-foreground" : "hover:bg-accent hover:text-accent-foreground"
      }`}
      data-testid={testId}
    >
      <FolderOpenIcon className="size-4 shrink-0 text-muted-foreground" />
      <span className="flex-1 truncate">{path}</span>
    </button>
  );
}