import { GUIDE_OFFSET, indentFor } from "./folderTreeConstants";

export function FolderTreeIndentGuides({ depth }: { depth: number }) {
  if (depth <= 0) return null;
  return (
    <>
      {Array.from({ length: depth }).map((_, i) => (
        <span
          // biome-ignore lint/suspicious/noArrayIndexKey: fixed positional guides
          key={i}
          aria-hidden
          className="pointer-events-none absolute top-0 bottom-0 w-px bg-border"
          style={{ left: `${indentFor(i) + GUIDE_OFFSET}px` }}
        />
      ))}
    </>
  );
}