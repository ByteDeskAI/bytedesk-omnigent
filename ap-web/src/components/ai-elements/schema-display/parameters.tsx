"use client";

import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { ChevronRightIcon } from "lucide-react";
import type { ComponentProps, HTMLAttributes } from "react";
import { useContext } from "react";

import { SchemaDisplayContext } from "./context";
import type { SchemaParameter } from "./types";

export type SchemaDisplayParameterProps = HTMLAttributes<HTMLDivElement> & SchemaParameter;

export const SchemaDisplayParameter = ({
  name,
  type,
  required,
  description,
  location,
  className,
  ...props
}: SchemaDisplayParameterProps) => (
  <div className={cn("px-4 py-3 pl-10", className)} {...props}>
    <div className="flex items-center gap-2">
      <span className="font-mono text-sm">{name}</span>
      <Badge className="text-xs" variant="outline">
        {type}
      </Badge>
      {location && (
        <Badge className="text-xs" variant="secondary">
          {location}
        </Badge>
      )}
      {required && (
        <Badge
          className="bg-red-100 text-red-700 text-xs dark:bg-red-900/30 dark:text-red-400"
          variant="secondary"
        >
          required
        </Badge>
      )}
    </div>
    {description && <p className="mt-1 text-muted-foreground text-sm">{description}</p>}
  </div>
);

export type SchemaDisplayParametersProps = ComponentProps<typeof Collapsible>;

export const SchemaDisplayParameters = ({
  className,
  children,
  ...props
}: SchemaDisplayParametersProps) => {
  const { parameters } = useContext(SchemaDisplayContext);

  return (
    <Collapsible className={cn(className)} defaultOpen {...props}>
      <CollapsibleTrigger className="group flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-muted/50">
        <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        <span className="font-medium text-sm">Parameters</span>
        <Badge className="ml-auto text-xs" variant="secondary">
          {parameters?.length}
        </Badge>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="divide-y border-t">
          {children ??
            parameters?.map((param) => <SchemaDisplayParameter key={param.name} {...param} />)}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
};