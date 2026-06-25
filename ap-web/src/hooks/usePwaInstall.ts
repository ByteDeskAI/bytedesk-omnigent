import { useCallback, useEffect, useState } from "react";
import {
  captureInstallPrompt,
  getDeferredInstallPrompt,
  triggerInstallPrompt,
} from "@/lib/pwa/installPrompt";
import { isStandaloneDisplayMode, shouldRegisterPwa } from "@/lib/pwa/runtime";

export function usePwaInstall(): {
  canInstall: boolean;
  isInstalled: boolean;
  install: () => Promise<"accepted" | "dismissed" | "unavailable">;
} {
  const [canInstall, setCanInstall] = useState(false);
  const [isInstalled, setIsInstalled] = useState(isStandaloneDisplayMode);

  useEffect(() => {
    if (!shouldRegisterPwa()) return;
    setIsInstalled(isStandaloneDisplayMode());
    const onInstallAvailable = (event: Event) => {
      captureInstallPrompt(event);
      setCanInstall(Boolean(getDeferredInstallPrompt()));
    };
    const onInstalled = () => {
      setCanInstall(false);
      setIsInstalled(true);
    };
    window.addEventListener("beforeinstallprompt", onInstallAvailable);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onInstallAvailable);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  const install = useCallback(async () => {
    const outcome = await triggerInstallPrompt();
    if (outcome === "accepted") setCanInstall(false);
    return outcome;
  }, []);

  return { canInstall, isInstalled, install };
}