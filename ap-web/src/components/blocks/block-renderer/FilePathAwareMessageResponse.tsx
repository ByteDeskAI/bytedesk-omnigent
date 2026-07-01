import type React from "react";
import { useMemo } from "react";
import { defaultRemarkPlugins } from "streamdown";
import remarkBreaks from "remark-breaks";
import { MessageResponse } from "@/components/ai-elements/message";
import { useThrottledValue } from "@/hooks/useThrottledValue";
import { cn } from "@/lib/utils";
import {
  useFileViewer,
  useFileViewerConversationId,
  useIsChangedPath,
  useWorkspacePaths,
} from "@/shell/FileViewerContext";
import { toWorkspaceRelativePath, useWorkspaceFileExists } from "@/hooks/useWorkspaceChangedFiles";
import {
  isPathologicalText,
  MAX_PLAINTEXT_DISPLAY_LENGTH,
  STREAM_MARKDOWN_THROTTLE_MS,
} from "./block-renderer-utils";

export function WorkspacePathInlineCode({
  children: codeChildren,
  className,
  ...codeProps
}: React.ComponentPropsWithoutRef<"code">) {
  const openFile = useFileViewer();
  const isChangedPath = useIsChangedPath();
  const conversationId = useFileViewerConversationId();
  const { root, home } = useWorkspacePaths();
  const text = typeof codeChildren === "string" ? codeChildren : "";

  // Collapse absolute / "~"-relative forms onto a workspace-relative path so
  // they match the changed-files list and the filesystem API. null = absolute
  // or "~" path outside the workspace (or the root itself) → never a link.
  const linkPath = text ? toWorkspaceRelativePath(text, root, home) : null;
  // "Trusted" means we resolved an absolute/"~" form against the root, so the
  // result is known workspace-relative even if it's a bare basename (no
  // interior slash) that the existence check's path-shape heuristic rejects.
  const trusted = linkPath !== null && linkPath !== text;

  const isChanged = !!linkPath && isChangedPath(linkPath);
  // Only hit the filesystem for path-shaped spans that aren't already known
  // changes; passing null disables the query (keeps hook order stable).
  const existsOnDisk = useWorkspaceFileExists(
    conversationId,
    openFile && linkPath && !isChanged ? linkPath : null,
    trusted,
  );

  if (openFile && linkPath && (isChanged || existsOnDisk)) {
    // Rendered as an inline <code> (not a <button>): a button is laid out as
    // an atomic inline-block, so a long path can't break across lines and
    // drops below the list marker as a whole unit. An inline <code> flows and
    // wraps like the surrounding text; role/tabIndex/keydown restore the
    // button semantics.
    return (
      <code
        role="button"
        tabIndex={0}
        data-streamdown="inline-code"
        // Keep the base inline-code class/props (merge, don't replace) so the
        // link only adds the underline affordance on top of Streamdown's
        // styling and any caller-provided attributes survive.
        className={cn(
          "font-mono text-sm underline decoration-dotted underline-offset-2 hover:text-foreground transition-colors cursor-pointer",
          className,
        )}
        onClick={() => openFile(linkPath)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            openFile(linkPath);
          }
        }}
        {...codeProps}
      >
        {codeChildren}
      </code>
    );
  }
  // Match Streamdown's default inline-code styling so non-path inline code
  // looks unchanged.
  return (
    <code
      className={cn("rounded bg-muted px-1.5 py-0.5 font-mono text-sm", className)}
      data-streamdown="inline-code"
      {...codeProps}
    >
      {codeChildren}
    </code>
  );
}

// Stable module-level override map so MessageResponse's memo (which ignores
// `components` changes) never sees a new identity.
const FILE_PATH_AWARE_COMPONENTS = { inlineCode: WorkspacePathInlineCode };

/**
 * Plain, break-anywhere fallback for a pathological text block — no markdown.
 * `whitespace-pre-wrap` keeps newlines; `break-all` gives the layout engine a
 * break opportunity inside an otherwise unbreakable token. Over-long payloads
 * are elided so the DOM node itself can't grow without bound.
 */
export function PlainTextFallback({ text }: { text: string }) {
  const truncated = text.length > MAX_PLAINTEXT_DISPLAY_LENGTH;
  const shown = truncated ? text.slice(0, MAX_PLAINTEXT_DISPLAY_LENGTH) : text;
  return (
    <div className="whitespace-pre-wrap break-all font-mono text-xs">
      {shown}
      {truncated && (
        <span className="text-muted-foreground">
          {`\n… [${text.length - MAX_PLAINTEXT_DISPLAY_LENGTH} more characters not shown]`}
        </span>
      )}
    </div>
  );
}

/**
 * Wraps `MessageResponse` with {@link WorkspacePathInlineCode} via Streamdown's
 * `inlineCode` slot — NOT `code` — so fenced code blocks keep their default
 * `<pre>` wrapper and Shiki highlighting. Overriding `code` here would replace
 * block rendering too, stripping `<pre>` and collapsing whitespace.
 *
 * When `breaks` is set, single newlines render as `<br>` (remark-breaks)
 * instead of collapsing to spaces per CommonMark. Used for user bubbles,
 * where people type multi-line messages without blank-line paragraph
 * separators and expect their line breaks preserved. NOTE: Streamdown's
 * `remarkPlugins` prop *replaces* its defaults rather than merging, so we
 * extend `defaultRemarkPlugins` (which carries remark-gfm) — passing
 * `[remarkBreaks]` alone would silently drop GFM tables / strikethrough.
 */
export function FilePathAwareMessageResponse({
  children,
  breaks = false,
  ...props
}: React.ComponentProps<typeof MessageResponse> & { breaks?: boolean }) {
  const components = FILE_PATH_AWARE_COMPONENTS;

  // Extend (don't replace) Streamdown's defaults so remark-gfm survives;
  // append remark-breaks only when `breaks` is requested. When `breaks` is
  // false we pass `undefined` so Streamdown uses its own defaults unchanged.
  const remarkPlugins = useMemo(
    () => (breaks ? [...Object.values(defaultRemarkPlugins), remarkBreaks] : undefined),
    [breaks],
  );

  // Throttle the markdown so the live (still-growing) bubble re-parses a few
  // times per second instead of on every store commit. `children` is a string
  // at both call sites (a text RenderItem and the user bubble); finalized/static
  // text changes once, which emits immediately, so this is a no-op off the
  // streaming path. The hook must be called unconditionally (rules of hooks), so
  // non-string children (none today) pass an inert "" and bypass the result.
  const isString = typeof children === "string";
  const throttledText = useThrottledValue(
    isString ? (children as string) : "",
    STREAM_MARKDOWN_THROTTLE_MS,
  );

  // Defense-in-depth: a string child that is huge or carries a
  // giant unbroken token (e.g. a base64 data URL serialized into the text
  // stream) would lock the tab in the markdown pipeline + layout. Render it as
  // plain break-anywhere text instead. Both call sites (assistant text blocks
  // and the user bubble) flow through here, so this one guard covers both.
  const pathological = useMemo(
    () => isString && isPathologicalText(children as string),
    [isString, children],
  );
  if (pathological) {
    return <PlainTextFallback text={children as string} />;
  }

  return (
    <MessageResponse {...props} components={components} remarkPlugins={remarkPlugins}>
      {isString ? throttledText : children}
    </MessageResponse>
  );
}

