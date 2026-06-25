import { DownloadIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { usePwaInstall } from "@/hooks/usePwaInstall";
import { shouldRegisterPwa } from "@/lib/pwa/runtime";

export function PwaInstallButton() {
  const { canInstall, isInstalled, install } = usePwaInstall();
  if (!shouldRegisterPwa() || isInstalled || !canInstall) return null;
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="hidden md:inline-flex"
      onClick={() => void install()}
      data-testid="pwa-install-button"
    >
      <DownloadIcon className="mr-1 size-4" />
      Install app
    </Button>
  );
}