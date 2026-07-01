"use client";

import { createContext, useContext } from "react";
import type { AttachmentContextValue, AttachmentsContextValue } from "./types";

export const AttachmentsContext = createContext<AttachmentsContextValue | null>(null);

export const AttachmentContext = createContext<AttachmentContextValue | null>(null);

export const useAttachmentsContext = () =>
  useContext(AttachmentsContext) ?? { variant: "grid" as const };

export const useAttachmentContext = () => {
  const ctx = useContext(AttachmentContext);
  if (!ctx) {
    throw new Error("Attachment components must be used within <Attachment>");
  }
  return ctx;
};