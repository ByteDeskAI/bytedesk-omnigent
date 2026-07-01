"use client";

import type { FileUIPart } from "ai";
import { nanoid } from "nanoid";
import type { RefObject } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useOptionalPromptInputController } from "./context";
import { validateIncomingFiles } from "./file-validation";
import type { AttachmentsContext, PromptInputProps } from "./types";

type UseAttachmentStateOptions = Pick<
  PromptInputProps,
  "accept" | "globalDrop" | "maxFiles" | "maxFileSize" | "onError" | "syncHiddenInput"
> & {
  formRef: RefObject<HTMLFormElement | null>;
};

export function useAttachmentState({
  accept,
  formRef,
  globalDrop,
  maxFiles,
  maxFileSize,
  onError,
  syncHiddenInput,
}: UseAttachmentStateOptions) {
  const controller = useOptionalPromptInputController();
  const usingProvider = !!controller;

  const inputRef = useRef<HTMLInputElement | null>(null);
  const [items, setItems] = useState<(FileUIPart & { id: string })[]>([]);
  const files = usingProvider ? controller.attachments.files : items;
  const filesRef = useRef(files);

  useEffect(() => {
    filesRef.current = files;
  }, [files]);

  const openFileDialogLocal = useCallback(() => {
    inputRef.current?.click();
  }, []);

  const addLocal = useCallback(
    (fileList: File[] | FileList) => {
      const incoming = [...fileList];

      setItems((prev) => {
        const capped = validateIncomingFiles(incoming, {
          accept,
          currentCount: prev.length,
          maxFileSize,
          maxFiles,
          onError,
        });
        if (capped.length === 0) {
          return prev;
        }

        return [
          ...prev,
          ...capped.map((file) => ({
            filename: file.name,
            id: nanoid(),
            mediaType: file.type,
            type: "file" as const,
            url: URL.createObjectURL(file),
          })),
        ];
      });
    },
    [accept, maxFileSize, maxFiles, onError],
  );

  const removeLocal = useCallback(
    (id: string) =>
      setItems((prev) => {
        const found = prev.find((file) => file.id === id);
        if (found?.url) {
          URL.revokeObjectURL(found.url);
        }
        return prev.filter((file) => file.id !== id);
      }),
    [],
  );

  const addWithProviderValidation = useCallback(
    (fileList: File[] | FileList) => {
      const capped = validateIncomingFiles([...fileList], {
        accept,
        currentCount: files.length,
        maxFileSize,
        maxFiles,
        onError,
      });
      if (capped.length > 0) {
        controller?.attachments.add(capped);
      }
    },
    [accept, maxFileSize, maxFiles, onError, files.length, controller],
  );

  const clearAttachments = useCallback(
    () =>
      usingProvider
        ? controller?.attachments.clear()
        : setItems((prev) => {
            for (const file of prev) {
              if (file.url) {
                URL.revokeObjectURL(file.url);
              }
            }
            return [];
          }),
    [usingProvider, controller],
  );

  const add = usingProvider ? addWithProviderValidation : addLocal;
  const remove = usingProvider ? controller.attachments.remove : removeLocal;
  const openFileDialog = usingProvider
    ? controller.attachments.openFileDialog
    : openFileDialogLocal;

  useEffect(() => {
    if (!usingProvider) {
      return;
    }
    controller.__registerFileInput(inputRef, () => inputRef.current?.click());
  }, [usingProvider, controller]);

  useEffect(() => {
    if (syncHiddenInput && inputRef.current && files.length === 0) {
      inputRef.current.value = "";
    }
  }, [files, syncHiddenInput]);

  useEffect(() => {
    const form = formRef.current;
    if (!form || globalDrop) {
      return;
    }

    const onDragOver = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        e.preventDefault();
      }
    };
    const onDrop = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        e.preventDefault();
      }
      if (e.dataTransfer?.files && e.dataTransfer.files.length > 0) {
        add(e.dataTransfer.files);
      }
    };
    form.addEventListener("dragover", onDragOver);
    form.addEventListener("drop", onDrop);
    return () => {
      form.removeEventListener("dragover", onDragOver);
      form.removeEventListener("drop", onDrop);
    };
  }, [add, formRef, globalDrop]);

  useEffect(() => {
    if (!globalDrop) {
      return;
    }

    const onDragOver = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        e.preventDefault();
      }
    };
    const onDrop = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        e.preventDefault();
      }
      if (e.dataTransfer?.files && e.dataTransfer.files.length > 0) {
        add(e.dataTransfer.files);
      }
    };
    document.addEventListener("dragover", onDragOver);
    document.addEventListener("drop", onDrop);
    return () => {
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("drop", onDrop);
    };
  }, [add, globalDrop]);

  useEffect(
    () => () => {
      if (!usingProvider) {
        for (const f of filesRef.current) {
          if (f.url) {
            URL.revokeObjectURL(f.url);
          }
        }
      }
    },
    [usingProvider],
  );

  const attachmentsCtx = useMemo<AttachmentsContext>(
    () => ({
      add,
      clear: clearAttachments,
      fileInputRef: inputRef,
      files: files.map((item) => ({ ...item, id: item.id })),
      openFileDialog,
      remove,
    }),
    [files, add, remove, clearAttachments, openFileDialog],
  );

  return {
    add,
    attachmentsCtx,
    clearAttachments,
    controller,
    files,
    inputRef: inputRef as RefObject<HTMLInputElement | null>,
    usingProvider,
  };
}