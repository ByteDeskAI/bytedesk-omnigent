export const MATCH_DISPLAY_LIMIT = 100;

/**
 * Split a typed path into the directory to list and the partial
 * basename to filter it by — drives the "Matches" autocomplete.
 */
export function splitTypedPath(input: string): { dir: string; partial: string } {
  const trimmed = input.trim();
  if (trimmed === "" || trimmed === "~") {
    return { dir: "", partial: "" };
  }
  const slash = trimmed.lastIndexOf("/");
  if (slash === -1) {
    return { dir: "", partial: trimmed };
  }
  const partial = trimmed.slice(slash + 1);
  let dir = trimmed.slice(0, slash);
  if (dir === "") {
    dir = "/";
  } else if (dir === "~") {
    dir = "";
  }
  return { dir, partial };
}