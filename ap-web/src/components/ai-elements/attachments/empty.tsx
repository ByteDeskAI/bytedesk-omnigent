"use client";

import { cn } from "@/lib/utils";
import type { HTMLAttributes } from "react";

export type AttachmentEmptyProps = HTMLAttributes<HTMLDivElement>;

export const AttachmentEmpty = ({ className, children, ...props }: AttachmentEmptyProps) => (
  <div
    className={cn("flex items-center justify-center p-4 text-muted-foreground text-sm", className)}
    {...props}
  >
    {children ?? "No attachments"}
  </div>
);