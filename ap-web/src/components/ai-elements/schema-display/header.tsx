"use client";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { ComponentProps, HTMLAttributes } from "react";
import { useContext } from "react";

import { methodStyles, SchemaDisplayContext } from "./context";

export type SchemaDisplayHeaderProps = HTMLAttributes<HTMLDivElement>;

export const SchemaDisplayHeader = ({
  className,
  children,
  ...props
}: SchemaDisplayHeaderProps) => (
  <div className={cn("flex items-center gap-3 border-b px-4 py-3", className)} {...props}>
    {children}
  </div>
);

export type SchemaDisplayMethodProps = ComponentProps<typeof Badge>;

export const SchemaDisplayMethod = ({
  className,
  children,
  ...props
}: SchemaDisplayMethodProps) => {
  const { method } = useContext(SchemaDisplayContext);

  return (
    <Badge
      className={cn("font-mono text-xs", methodStyles[method], className)}
      variant="secondary"
      {...props}
    >
      {children ?? method}
    </Badge>
  );
};

export type SchemaDisplayPathProps = HTMLAttributes<HTMLSpanElement>;

export const SchemaDisplayPath = ({ className, children, ...props }: SchemaDisplayPathProps) => {
  const { path } = useContext(SchemaDisplayContext);

  // Highlight path parameters
  const highlightedPath = path.replaceAll(
    /\{([^}]+)\}/g,
    '<span class="text-blue-600 dark:text-blue-400">{$1}</span>',
  );

  return (
    <span
      className={cn("font-mono text-sm", className)}
      // oxlint-disable-next-line eslint-plugin-react(no-danger)
      dangerouslySetInnerHTML={{ __html: children ?? highlightedPath }}
      {...props}
    />
  );
};

export type SchemaDisplayDescriptionProps = HTMLAttributes<HTMLParagraphElement>;

export const SchemaDisplayDescription = ({
  className,
  children,
  ...props
}: SchemaDisplayDescriptionProps) => {
  const { description } = useContext(SchemaDisplayContext);

  return (
    <p className={cn("border-b px-4 py-3 text-muted-foreground text-sm", className)} {...props}>
      {children ?? description}
    </p>
  );
};