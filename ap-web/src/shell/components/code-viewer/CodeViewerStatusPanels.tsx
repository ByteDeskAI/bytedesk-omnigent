import { isBinaryPath } from "../../codeViewerHelpers";
import type { useFileContent } from "@/hooks/useFileContent";

export function CodeViewerLoadingPanel() {
  return (
    <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
      Loading…
    </div>
  );
}

export function CodeViewerErrorPanel({ fileQuery }: { fileQuery: ReturnType<typeof useFileContent> }) {
  return (
    <div className="p-8 text-destructive text-sm">
      Error loading file:{" "}
      {fileQuery.error instanceof Error ? fileQuery.error.message : String(fileQuery.error)}
    </div>
  );
}

export function CodeViewerBinaryPanel() {
  return (
    <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
      Preview not available for binary files.
    </div>
  );
}

export function CodeViewerStatusRouter({
  path,
  fileQuery,
}: {
  path: string;
  fileQuery: ReturnType<typeof useFileContent>;
}) {
  if (fileQuery.isLoading) return <CodeViewerLoadingPanel />;
  if (fileQuery.isError) return <CodeViewerErrorPanel fileQuery={fileQuery} />;
  if (fileQuery.data?.encoding === "base64" || isBinaryPath(path)) return <CodeViewerBinaryPanel />;
  return null;
}