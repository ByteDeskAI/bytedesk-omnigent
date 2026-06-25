import { getOmnigentHostConfig } from "@/lib/host";
import { isNativeShell } from "@/lib/nativeBridge";

/** True when ap-web runs inside a host embed (Databricks monolith). */
export function isEmbedMode(): boolean {
  if (typeof window === "undefined") return false;
  return Boolean(getOmnigentHostConfig().fetcher);
}

/** Standalone browser tab — not Electron, not embedded. */
export function shouldRegisterPwa(): boolean {
  if (typeof window === "undefined") return false;
  return !isEmbedMode() && !isNativeShell();
}

/** Installed PWA or iOS Add to Home Screen. */
export function isStandaloneDisplayMode(): boolean {
  if (typeof window === "undefined") return false;
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    window.matchMedia("(display-mode: minimal-ui)").matches ||
    // iOS legacy
    ("standalone" in navigator && (navigator as Navigator & { standalone?: boolean }).standalone === true)
  );
}

/** Desktop installed PWA with Window Controls Overlay support. */
export function supportsWindowControlsOverlay(): boolean {
  if (typeof window === "undefined") return false;
  return isStandaloneDisplayMode() && "windowControlsOverlay" in navigator;
}