import type { ReactNode } from "react";
import { DownloadIcon, FileIcon } from "lucide-react";
import { SessionImage } from "@/components/SessionImage";
import type { RenderItem } from "@/lib/renderItems";
import { ApprovalCard } from "../ApprovalCard";
import { ReasoningView } from "../ReasoningView";
import { SlashCommandCard } from "../SlashCommandCard";
import { TerminalCommandCard } from "../TerminalCommandCard";
import { ErrorBanner, PolicyDeniedBanner, RetryIndicator } from "../StatusBlocks";
import { ToolCard } from "../tool-card";
import { keyFor } from "./block-renderer-utils";
import { FilePathAwareMessageResponse } from "./FilePathAwareMessageResponse";

export function renderItem(
  item: RenderItem,
  index: number,
  isReasoningStreaming: boolean,
  conversationId: string | null,
): ReactNode {
  const key = keyFor(item, index);
  switch (item.kind) {
    case "text":
      return <FilePathAwareMessageResponse key={key}>{item.text}</FilePathAwareMessageResponse>;
    case "reasoning":
      return (
        <ReasoningView
          key={key}
          text={item.text}
          isStreaming={isReasoningStreaming}
          duration={item.duration}
        />
      );
    case "tool":
      return (
        <ToolCard
          key={key}
          name={item.execution.name}
          argsSummary={item.execution.argsSummary}
          arguments={item.execution.arguments}
          output={item.output}
          state={item.state}
          startedAt={item.startedAt}
          duration={item.duration}
        />
      );
    case "native_tool":
      // Reuse the same tool card. Native tools are server-side
      // (provider-managed) so they're always "completed" by the
      // time we see them; render the raw provider data as input.
      return (
        <ToolCard
          key={key}
          name={item.label}
          nativeToolType={item.toolType}
          arguments={item.data}
          output={null}
          state="output-available"
        />
      );
    case "file":
      return <FileArtifact key={key} item={item} conversationId={conversationId} />;
    case "slash_command":
      return (
        <SlashCommandCard
          key={key}
          kind={item.slashKind}
          name={item.name}
          arguments={item.arguments}
          output={item.output}
        />
      );
    case "terminal_command":
      return (
        <TerminalCommandCard
          key={key}
          kind={item.terminalKind}
          input={item.input}
          stdout={item.stdout}
          stderr={item.stderr}
        />
      );
    case "error":
      return <ErrorBanner key={key} message={item.message} source={item.source} code={item.code} />;
    case "policy_denied":
      return <PolicyDeniedBanner key={key} reason={item.reason} phase={item.phase} />;
    case "retry":
      return (
        <RetryIndicator
          key={key}
          source={item.source}
          attempt={item.attempt}
          maxAttempts={item.maxAttempts}
          delaySeconds={item.delaySeconds}
        />
      );
    case "elicitation":
      return (
        <ApprovalCard
          key={key}
          elicitationId={item.elicitationId}
          message={item.message}
          phase={item.phase}
          policyName={item.policyName}
          contentPreview={item.contentPreview}
          requestedSchema={item.requestedSchema}
          url={item.url}
          status={item.status}
          response={item.response}
          askUserQuestion={item.askUserQuestion}
          exitPlanMode={item.exitPlanMode}
          codexCommand={item.codexCommand}
          allowAllEdits={item.allowAllEdits}
        />
      );
  }
}

export function FileArtifact({
  item,
  conversationId,
}: {
  item: Extract<RenderItem, { kind: "file" }>;
  conversationId: string | null;
}) {
  const label = item.filename ?? item.fileId;
  const path = conversationId
    ? `/v1/sessions/${encodeURIComponent(conversationId)}/resources/files/${encodeURIComponent(
        item.fileId,
      )}/content`
    : undefined;
  const isImage = isImageArtifact(item);

  if (isImage && path) {
    return (
      <div className="not-prose space-y-1.5" data-testid="assistant-file-artifact">
        <SessionImage
          path={path}
          alt={label}
          className="max-h-80 max-w-full rounded-md border border-border object-contain"
        />
        <ArtifactLabel label={label} path={path} />
      </div>
    );
  }

  const content = (
    <>
      <FileIcon className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 truncate">{label}</span>
      {path && <DownloadIcon className="size-3.5 shrink-0 opacity-70" aria-hidden="true" />}
    </>
  );

  if (!path) {
    return (
      <div
        className="not-prose inline-flex max-w-full items-center gap-1.5 rounded-md border border-border bg-muted px-2.5 py-1.5 text-muted-foreground text-xs"
        data-testid="assistant-file-artifact"
      >
        {content}
      </div>
    );
  }

  return (
    <a
      className="not-prose inline-flex max-w-full items-center gap-1.5 rounded-md border border-border bg-muted px-2.5 py-1.5 text-muted-foreground text-xs transition-colors hover:text-foreground"
      data-testid="assistant-file-artifact"
      href={path}
      download={item.filename ?? undefined}
    >
      {content}
    </a>
  );
}

function ArtifactLabel({ label, path }: { label: string; path: string }) {
  return (
    <a
      className="inline-flex max-w-full items-center gap-1.5 text-muted-foreground text-xs transition-colors hover:text-foreground"
      href={path}
      download={label}
    >
      <DownloadIcon className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 truncate">{label}</span>
    </a>
  );
}

function isImageArtifact(item: Extract<RenderItem, { kind: "file" }>): boolean {
  if (item.contentType?.startsWith("image/")) return true;
  return /\.(png|jpe?g|webp|gif|svg)$/i.test(item.filename ?? "");
}
