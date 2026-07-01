"use client";

import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import { CheckIcon, CopyIcon } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import {
  cloneElement,
  isValidElement,
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Streamdown, type StreamdownProps } from "streamdown";

import {
  CHAT_LINK_SAFETY,
  SECURE_STREAMDOWN_REHYPE_PLUGINS,
  STREAMDOWN_PLUGINS,
} from "../streamdown-security";

export type MessageResponseProps = Omit<StreamdownProps, "rehypePlugins">;

function getChatCodeControls(controls: StreamdownProps["controls"]): StreamdownProps["controls"] {
  if (typeof controls === "object" && controls !== null) {
    const codeControls = controls.code;
    return {
      ...controls,
      code: {
        ...(typeof codeControls === "object" && codeControls !== null ? codeControls : {}),
        copy: false,
        download: true,
      },
    };
  }

  return { code: { copy: false, download: true } };
}

function extractCodeText(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") {
    return String(children);
  }

  if (Array.isArray(children)) {
    return children.map(extractCodeText).join("");
  }

  if (isValidElement(children)) {
    const props = children.props as { children?: ReactNode; code?: unknown };
    if (typeof props.code === "string") {
      return props.code;
    }
    return extractCodeText(props.children);
  }

  return "";
}

function ChatCodeBlockCopyButton({ getCode }: { getCode: () => string }) {
  const [isCopied, setIsCopied] = useState(false);
  const timeoutRef = useRef<number>(0);

  const handleClick = useCallback(() => {
    if (isCopied) return;

    try {
      const copyResult = copyText(getCode());
      void copyResult.then(
        () => {
          setIsCopied(true);
          timeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
        },
        (error) => {
          console.warn("Failed to copy code block", error);
        },
      );
    } catch (error) {
      console.warn("Failed to copy code block", error);
    }
  }, [getCode, isCopied]);

  useEffect(
    () => () => {
      window.clearTimeout(timeoutRef.current);
    },
    [],
  );

  const Icon = isCopied ? CheckIcon : CopyIcon;

  return (
    <Button
      aria-label="Copy Code"
      className="absolute top-2 right-12 z-10 size-8 bg-sidebar/80 text-muted-foreground hover:text-foreground supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur"
      onClick={handleClick}
      size="icon-sm"
      title="Copy Code"
      type="button"
      variant="ghost"
    >
      <Icon size={14} />
    </Button>
  );
}

function ChatCodeBlockPre({ children }: ComponentProps<"pre">) {
  const code = extractCodeText(children);
  const getCode = useCallback(() => code, [code]);
  const block = isValidElement(children)
    ? cloneElement(children, { "data-block": "true" } as Record<string, unknown>)
    : children;

  return (
    <div className="relative">
      {block}
      <ChatCodeBlockCopyButton getCode={getCode} />
    </div>
  );
}

export const MessageResponse = memo(
  ({ className, components, controls, ...props }: MessageResponseProps) => {
    const messageComponents = useMemo(
      () => ({ ...components, pre: ChatCodeBlockPre }),
      [components],
    );

    const messageControls = useMemo(() => getChatCodeControls(controls), [controls]);

    return (
      <Streamdown
        className={cn("size-full [&>*:first-child]:mt-0 [&>*:last-child]:mb-0", className)}
        plugins={STREAMDOWN_PLUGINS}
        // Let links open on a plain click (and cmd/ctrl-click in a new tab)
        // instead of Streamdown's default "Open external link?" modal.
        linkSafety={CHAT_LINK_SAFETY}
        {...props}
        components={messageComponents}
        controls={messageControls}
        // Block remote image fetches that can exfiltrate data through URLs.
        rehypePlugins={SECURE_STREAMDOWN_REHYPE_PLUGINS}
      />
    );
  },
  (prevProps, nextProps) =>
    prevProps.children === nextProps.children && nextProps.isAnimating === prevProps.isAnimating,
);

MessageResponse.displayName = "MessageResponse";