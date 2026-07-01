"use client";

import { InputGroup } from "@/components/ui/input-group";
import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

import {
  LocalAttachmentsContext,
  LocalReferencedSourcesContext,
} from "./context";
import type { PromptInputProps } from "./types";
import { usePromptInputForm } from "./use-prompt-input-form";

export const PromptInput = ({
  className,
  accept,
  multiple,
  globalDrop,
  syncHiddenInput,
  maxFiles,
  maxFileSize,
  onError,
  onSubmit,
  children,
  ...props
}: PromptInputProps) => {
  const { attachmentsCtx, formRef, handleChange, handleSubmit, inputRef, refsCtx } =
    usePromptInputForm({
      accept,
      globalDrop,
      maxFiles,
      maxFileSize,
      onError,
      onSubmit,
      syncHiddenInput,
    });

  const inner: ReactNode = (
    <>
      <input
        accept={accept}
        aria-label="Upload files"
        className="hidden"
        multiple={multiple}
        onChange={handleChange}
        ref={inputRef}
        title="Upload files"
        type="file"
      />
      <form className={cn("w-full", className)} onSubmit={handleSubmit} ref={formRef} {...props}>
        <InputGroup className="overflow-hidden">{children}</InputGroup>
      </form>
    </>
  );

  const withReferencedSources = (
    <LocalReferencedSourcesContext.Provider value={refsCtx}>
      {inner}
    </LocalReferencedSourcesContext.Provider>
  );

  return (
    <LocalAttachmentsContext.Provider value={attachmentsCtx}>
      {withReferencedSources}
    </LocalAttachmentsContext.Provider>
  );
};