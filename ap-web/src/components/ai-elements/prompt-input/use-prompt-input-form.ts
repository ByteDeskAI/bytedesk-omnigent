"use client";

import type { FileUIPart, SourceDocumentUIPart } from "ai";
import { nanoid } from "nanoid";
import type { ChangeEventHandler, FormEventHandler } from "react";
import { useCallback, useMemo, useRef, useState } from "react";

import { convertBlobUrlToDataUrl } from "./helpers";
import type { PromptInputProps, ReferencedSourcesContext } from "./types";
import { useAttachmentState } from "./use-attachment-state";

type UsePromptInputFormOptions = Pick<
  PromptInputProps,
  "accept" | "globalDrop" | "maxFiles" | "maxFileSize" | "onError" | "onSubmit" | "syncHiddenInput"
>;

export function usePromptInputForm({
  accept,
  globalDrop,
  maxFiles,
  maxFileSize,
  onError,
  onSubmit,
  syncHiddenInput,
}: UsePromptInputFormOptions) {
  const formRef = useRef<HTMLFormElement | null>(null);

  const {
    attachmentsCtx,
    clearAttachments,
    controller,
    files,
    inputRef,
    usingProvider,
  } = useAttachmentState({
    accept,
    formRef,
    globalDrop,
    maxFiles,
    maxFileSize,
    onError,
    syncHiddenInput,
  });

  const [referencedSources, setReferencedSources] = useState<
    (SourceDocumentUIPart & { id: string })[]
  >([]);

  const clearReferencedSources = useCallback(() => setReferencedSources([]), []);

  const clear = useCallback(() => {
    clearAttachments();
    clearReferencedSources();
  }, [clearAttachments, clearReferencedSources]);

  const handleChange: ChangeEventHandler<HTMLInputElement> = useCallback(
    (event) => {
      if (event.currentTarget.files) {
        attachmentsCtx.add(event.currentTarget.files);
      }
      event.currentTarget.value = "";
    },
    [attachmentsCtx],
  );

  const refsCtx = useMemo<ReferencedSourcesContext>(
    () => ({
      add: (incoming: SourceDocumentUIPart[] | SourceDocumentUIPart) => {
        const array = Array.isArray(incoming) ? incoming : [incoming];
        setReferencedSources((prev) => [...prev, ...array.map((s) => ({ ...s, id: nanoid() }))]);
      },
      clear: clearReferencedSources,
      remove: (id: string) => {
        setReferencedSources((prev) => prev.filter((s) => s.id !== id));
      },
      sources: referencedSources,
    }),
    [referencedSources, clearReferencedSources],
  );

  const handleSubmit: FormEventHandler<HTMLFormElement> = useCallback(
    async (event) => {
      event.preventDefault();

      const form = event.currentTarget;
      const text = usingProvider
        ? controller!.textInput.value
        : (() => {
            const formData = new FormData(form);
            return (formData.get("message") as string) || "";
          })();

      if (!usingProvider) {
        form.reset();
      }

      try {
        const convertedFiles: FileUIPart[] = await Promise.all(
          files.map(async ({ id: _id, ...item }) => {
            if (item.url?.startsWith("blob:")) {
              const dataUrl = await convertBlobUrlToDataUrl(item.url);
              return {
                ...item,
                url: dataUrl ?? item.url,
              };
            }
            return item;
          }),
        );

        const result = onSubmit({ files: convertedFiles, text }, event);

        if (result instanceof Promise) {
          try {
            await result;
            clear();
            if (usingProvider) {
              controller!.textInput.clear();
            }
          } catch {
            // Don't clear on error - user may want to retry
          }
        } else {
          clear();
          if (usingProvider) {
            controller!.textInput.clear();
          }
        }
      } catch {
        // Don't clear on error - user may want to retry
      }
    },
    [usingProvider, controller, files, onSubmit, clear],
  );

  return {
    attachmentsCtx,
    formRef,
    handleChange,
    handleSubmit,
    inputRef,
    refsCtx,
  };
}