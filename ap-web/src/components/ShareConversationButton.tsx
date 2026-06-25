import { Share2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";

interface ShareConversationButtonProps {
  title: string;
  url: string;
  excerpt?: string;
}

async function copyToClipboard(text: string): Promise<void> {
  await navigator.clipboard.writeText(text);
}

export function ShareConversationButton({ title, url, excerpt }: ShareConversationButtonProps) {
  const handleShare = async () => {
    const text = excerpt?.trim() ? excerpt : title;
    if (typeof navigator.share === "function") {
      try {
        await navigator.share({ title, text, url });
        return;
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
      }
    }
    await copyToClipboard(url);
  };

  return (
    <Button type="button" variant="ghost" size="icon" aria-label="Share" onClick={() => void handleShare()}>
      <Share2Icon className="size-4" />
    </Button>
  );
}