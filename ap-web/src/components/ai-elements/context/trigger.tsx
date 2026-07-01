"use client";

import { Button } from "@/components/ui/button";
import { HoverCardTrigger } from "@/components/ui/hover-card";
import type { ComponentProps } from "react";
import { useContextValue } from "./context";
import { ContextIcon } from "./icon";

export type ContextTriggerProps = ComponentProps<typeof Button>;

export const ContextTrigger = ({ children, ...props }: ContextTriggerProps) => {
  const { usedTokens, maxTokens } = useContextValue();
  const usedPercent = usedTokens / maxTokens;
  const renderedPercent = new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 1,
    style: "percent",
  }).format(usedPercent);

  return (
    <HoverCardTrigger asChild>
      {children ?? (
        <Button type="button" variant="ghost" {...props}>
          <span className="font-medium text-muted-foreground">{renderedPercent}</span>
          <ContextIcon />
        </Button>
      )}
    </HoverCardTrigger>
  );
};