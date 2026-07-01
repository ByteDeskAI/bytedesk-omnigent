import { useEffect, useRef } from "react";
import { FileIcon, XIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export function FileTabsStrip({
  openFiles,
  activeFilePath,
  onFileSelect,
  onCloseFile,
}: {
  openFiles: string[];
  activeFilePath: string | null;
  onFileSelect: (path: string) => void;
  onCloseFile: (path: string) => void;
}) {
  const activeTabRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    activeTabRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeFilePath]);

  return (
    <div className="flex items-center gap-0.5">
      {openFiles.map((path) => {
        const name = path.split("/").pop() ?? path;
        const active = path === activeFilePath;
        return (
          <div
            key={path}
            ref={active ? activeTabRef : undefined}
            role="button"
            tabIndex={0}
            aria-current={active}
            title={path}
            onClick={() => onFileSelect(path)}
            onAuxClick={(e) => {
              if (e.button === 1) {
                e.preventDefault();
                onCloseFile(path);
              }
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onFileSelect(path);
              }
            }}
            className={cn(
              "group/tab relative flex h-[32px] min-w-0 max-w-[320px] shrink-0 cursor-pointer items-center justify-center gap-[6px] overflow-hidden rounded-[8px] px-[12px] text-[13px] font-medium leading-5 transition-colors",
              active
                ? "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <FileIcon className="size-4 shrink-0" />
            <span className="min-w-0 truncate">{name}</span>
            <span
              className={cn(
                "absolute inset-y-0 right-[2px] flex items-center pl-[12px] pr-[4px] opacity-0 transition-opacity group-hover/tab:opacity-100",
                active
                  ? "[background:linear-gradient(to_right,transparent,color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))_40%)]"
                  : "[background:linear-gradient(to_right,transparent,var(--card)_40%)]",
              )}
            >
              <button
                type="button"
                aria-label={`Close ${name}`}
                className="flex size-6 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                onClick={(e) => {
                  e.stopPropagation();
                  onCloseFile(path);
                }}
              >
                <XIcon className="size-4" />
              </button>
            </span>
          </div>
        );
      })}
    </div>
  );
}