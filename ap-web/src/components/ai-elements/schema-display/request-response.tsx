"use client";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { ChevronRightIcon } from "lucide-react";
import type { ComponentProps } from "react";
import { useContext } from "react";

import { SchemaDisplayContext } from "./context";
import { SchemaDisplayProperty } from "./property";

export type SchemaDisplayRequestProps = ComponentProps<typeof Collapsible>;

export const SchemaDisplayRequest = ({
  className,
  children,
  ...props
}: SchemaDisplayRequestProps) => {
  const { requestBody } = useContext(SchemaDisplayContext);

  return (
    <Collapsible className={cn(className)} defaultOpen {...props}>
      <CollapsibleTrigger className="group flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-muted/50">
        <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        <span className="font-medium text-sm">Request Body</span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="border-t">
          {children ??
            requestBody?.map((prop) => (
              <SchemaDisplayProperty key={prop.name} {...prop} depth={0} />
            ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
};

export type SchemaDisplayResponseProps = ComponentProps<typeof Collapsible>;

export const SchemaDisplayResponse = ({
  className,
  children,
  ...props
}: SchemaDisplayResponseProps) => {
  const { responseBody } = useContext(SchemaDisplayContext);

  return (
    <Collapsible className={cn(className)} defaultOpen {...props}>
      <CollapsibleTrigger className="group flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-muted/50">
        <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        <span className="font-medium text-sm">Response</span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="border-t">
          {children ??
            responseBody?.map((prop) => (
              <SchemaDisplayProperty key={prop.name} {...prop} depth={0} />
            ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
};