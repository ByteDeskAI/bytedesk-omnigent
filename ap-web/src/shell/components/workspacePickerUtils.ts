/**
 * Compute the parent directory of an absolute path.
 *
 * Returns ``null`` when the input is empty (host's home view —
 * has no parent in the picker's UX) or already at the root
 * ``"/"``. Otherwise drops the last segment.
 */
export function parentOf(absolutePath: string): string | null {
  if (absolutePath === "" || absolutePath === "/") {
    return null;
  }
  const stripped = absolutePath.endsWith("/") ? absolutePath.slice(0, -1) : absolutePath;
  const idx = stripped.lastIndexOf("/");
  if (idx <= 0) {
    return "/";
  }
  return stripped.slice(0, idx);
}

/**
 * Normalize a path the user typed into the path input.
 */
export function normalizeTypedPath(input: string, home: string | null = null): string | null {
  const trimmed = input.trim();
  if (trimmed === "") {
    return null;
  }
  let absolute: string;
  if (trimmed === "~") {
    if (home === null) return null;
    absolute = home;
  } else if (trimmed.startsWith("~/")) {
    if (home === null) return null;
    absolute = `${home}/${trimmed.slice(2)}`;
  } else if (trimmed.startsWith("/")) {
    absolute = trimmed;
  } else {
    return null;
  }
  const collapsed = absolute.replace(/\/+/g, "/");
  if (collapsed === "/") {
    return "/";
  }
  return collapsed.endsWith("/") ? collapsed.slice(0, -1) : collapsed;
}

/** Basename of an absolute path, for the "Select current" label. */
export function basename(absolutePath: string): string {
  if (absolutePath === "") {
    return "~";
  }
  if (absolutePath === "/") {
    return "/";
  }
  const parts = absolutePath.split("/").filter((p) => p.length > 0);
  return parts[parts.length - 1] ?? absolutePath;
}

/** True when a path can be opened in the picker. */
export function isNavigablePath(path: string): boolean {
  const trimmed = path.trim();
  return trimmed.startsWith("/") || trimmed === "~" || trimmed.startsWith("~/");
}

/** Live filter for the listing, derived from the path-bar text. */
export function listingFilter(
  pathInput: string,
  currentAbsolute: string,
  home: string | null = null,
): string | null {
  const trimmed = pathInput.trim();
  if (trimmed === "") return null;
  const slash = trimmed.lastIndexOf("/");
  if (slash === -1) {
    return trimmed;
  }
  const partial = trimmed.slice(slash + 1);
  if (partial === "") return null;
  const dirText = trimmed.slice(0, slash) || "/";
  return normalizeTypedPath(dirText, home) === currentAbsolute ? partial : null;
}