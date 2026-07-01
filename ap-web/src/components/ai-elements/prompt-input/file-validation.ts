export function matchesFileAccept(file: File, accept?: string): boolean {
  if (!accept || accept.trim() === "") {
    return true;
  }

  const patterns = accept
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  return patterns.some((pattern) => {
    if (pattern.endsWith("/*")) {
      const prefix = pattern.slice(0, -1);
      return file.type.startsWith(prefix);
    }
    return file.type === pattern;
  });
}

export type FileValidationError = {
  code: "max_files" | "max_file_size" | "accept";
  message: string;
};

export function validateIncomingFiles(
  incoming: File[],
  options: {
    accept?: string;
    maxFiles?: number;
    maxFileSize?: number;
    currentCount?: number;
    onError?: (err: FileValidationError) => void;
  },
): File[] {
  const { accept, maxFiles, maxFileSize, currentCount = 0, onError } = options;

  const accepted = incoming.filter((f) => matchesFileAccept(f, accept));
  if (incoming.length && accepted.length === 0) {
    onError?.({
      code: "accept",
      message: "No files match the accepted types.",
    });
    return [];
  }

  const withinSize = (f: File) => (maxFileSize ? f.size <= maxFileSize : true);
  const sized = accepted.filter(withinSize);
  if (accepted.length > 0 && sized.length === 0) {
    onError?.({
      code: "max_file_size",
      message: "All files exceed the maximum size.",
    });
    return [];
  }

  const capacity =
    typeof maxFiles === "number" ? Math.max(0, maxFiles - currentCount) : undefined;
  const capped = typeof capacity === "number" ? sized.slice(0, capacity) : sized;
  if (typeof capacity === "number" && sized.length > capacity) {
    onError?.({
      code: "max_files",
      message: "Too many files. Some were not added.",
    });
  }

  return capped;
}