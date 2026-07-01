"use client";

import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { ExternalLinkIcon } from "lucide-react";
import type { ComponentProps } from "react";
import { useOpenInContext } from "./context";
import { providers } from "./providers";

export type OpenInChatGPTProps = ComponentProps<typeof DropdownMenuItem>;

export const OpenInChatGPT = (props: OpenInChatGPTProps) => {
  const { query } = useOpenInContext();
  return (
    <DropdownMenuItem asChild {...props}>
      <a
        className="flex items-center gap-2"
        href={providers.chatgpt.createUrl(query)}
        rel="noopener"
        target="_blank"
      >
        <span className="shrink-0">{providers.chatgpt.icon}</span>
        <span className="flex-1">{providers.chatgpt.title}</span>
        <ExternalLinkIcon className="size-4 shrink-0" />
      </a>
    </DropdownMenuItem>
  );
};

export type OpenInClaudeProps = ComponentProps<typeof DropdownMenuItem>;

export const OpenInClaude = (props: OpenInClaudeProps) => {
  const { query } = useOpenInContext();
  return (
    <DropdownMenuItem asChild {...props}>
      <a
        className="flex items-center gap-2"
        href={providers.claude.createUrl(query)}
        rel="noopener"
        target="_blank"
      >
        <span className="shrink-0">{providers.claude.icon}</span>
        <span className="flex-1">{providers.claude.title}</span>
        <ExternalLinkIcon className="size-4 shrink-0" />
      </a>
    </DropdownMenuItem>
  );
};

export type OpenInT3Props = ComponentProps<typeof DropdownMenuItem>;

export const OpenInT3 = (props: OpenInT3Props) => {
  const { query } = useOpenInContext();
  return (
    <DropdownMenuItem asChild {...props}>
      <a
        className="flex items-center gap-2"
        href={providers.t3.createUrl(query)}
        rel="noopener"
        target="_blank"
      >
        <span className="shrink-0">{providers.t3.icon}</span>
        <span className="flex-1">{providers.t3.title}</span>
        <ExternalLinkIcon className="size-4 shrink-0" />
      </a>
    </DropdownMenuItem>
  );
};

export type OpenInSciraProps = ComponentProps<typeof DropdownMenuItem>;

export const OpenInScira = (props: OpenInSciraProps) => {
  const { query } = useOpenInContext();
  return (
    <DropdownMenuItem asChild {...props}>
      <a
        className="flex items-center gap-2"
        href={providers.scira.createUrl(query)}
        rel="noopener"
        target="_blank"
      >
        <span className="shrink-0">{providers.scira.icon}</span>
        <span className="flex-1">{providers.scira.title}</span>
        <ExternalLinkIcon className="size-4 shrink-0" />
      </a>
    </DropdownMenuItem>
  );
};

export type OpenInv0Props = ComponentProps<typeof DropdownMenuItem>;

export const OpenInv0 = (props: OpenInv0Props) => {
  const { query } = useOpenInContext();
  return (
    <DropdownMenuItem asChild {...props}>
      <a
        className="flex items-center gap-2"
        href={providers.v0.createUrl(query)}
        rel="noopener"
        target="_blank"
      >
        <span className="shrink-0">{providers.v0.icon}</span>
        <span className="flex-1">{providers.v0.title}</span>
        <ExternalLinkIcon className="size-4 shrink-0" />
      </a>
    </DropdownMenuItem>
  );
};

export type OpenInCursorProps = ComponentProps<typeof DropdownMenuItem>;

export const OpenInCursor = (props: OpenInCursorProps) => {
  const { query } = useOpenInContext();
  return (
    <DropdownMenuItem asChild {...props}>
      <a
        className="flex items-center gap-2"
        href={providers.cursor.createUrl(query)}
        rel="noopener"
        target="_blank"
      >
        <span className="shrink-0">{providers.cursor.icon}</span>
        <span className="flex-1">{providers.cursor.title}</span>
        <ExternalLinkIcon className="size-4 shrink-0" />
      </a>
    </DropdownMenuItem>
  );
};