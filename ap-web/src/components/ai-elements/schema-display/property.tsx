"use client";

import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { ChevronRightIcon } from "lucide-react";
import type { HTMLAttributes } from "react";

import type { SchemaProperty } from "./types";

export type SchemaDisplayPropertyProps = HTMLAttributes<HTMLDivElement> &
  SchemaProperty & {
    depth?: number;
  };

export const SchemaDisplayProperty = ({
  name,
  type,
  required,
  description,
  properties,
  items,
  depth = 0,
  className,
  ...props
}: SchemaDisplayPropertyProps) => {
  const hasChildren = properties || items;
  const paddingLeft = 40 + depth * 16;

  if (hasChildren) {
    return (
      <Collapsible defaultOpen={depth < 2}>
        <CollapsibleTrigger
          className={cn(
            "group flex w-full items-center gap-2 py-3 text-left transition-colors hover:bg-muted/50",
            className,
          )}
          style={{ paddingLeft }}
        >
          <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
          <span className="font-mono text-sm">{name}</span>
          <Badge className="text-xs" variant="outline">
            {type}
          </Badge>
          {required && (
            <Badge
              className="bg-red-100 text-red-700 text-xs dark:bg-red-900/30 dark:text-red-400"
              variant="secondary"
            >
              required
            </Badge>
          )}
        </CollapsibleTrigger>
        {description && (
          <p
            className="pb-2 text-muted-foreground text-sm"
            style={{ paddingLeft: paddingLeft + 24 }}
          >
            {description}
          </p>
        )}
        <CollapsibleContent>
          <div className="divide-y border-t">
            {properties?.map((prop) => (
              <SchemaDisplayProperty key={prop.name} {...prop} depth={depth + 1} />
            ))}
            {items && <SchemaDisplayProperty {...items} depth={depth + 1} name={`${name}[]`} />}
          </div>
        </CollapsibleContent>
      </Collapsible>
    );
  }

  return (
    <div className={cn("py-3 pr-4", className)} style={{ paddingLeft }} {...props}>
      <div className="flex items-center gap-2">
        {/* Spacer for alignment */}
        <span className="size-4" />
        <span className="font-mono text-sm">{name}</span>
        <Badge className="text-xs" variant="outline">
          {type}
        </Badge>
        {required && (
          <Badge
            className="bg-red-100 text-red-700 text-xs dark:bg-red-900/30 dark:text-red-400"
            variant="secondary"
          >
            required
          </Badge>
        )}
      </div>
      {description && <p className="mt-1 pl-6 text-muted-foreground text-sm">{description}</p>}
    </div>
  );
};