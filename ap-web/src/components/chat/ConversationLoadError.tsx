import { Button } from "@/components/ui/button";
import { useNavigate } from "@/lib/routing";

/**
 * Error state for `/c/:id` when the items endpoint fails.
 */
export function ConversationLoadError({
  conversationId,
  error,
}: {
  conversationId: string;
  error: Error;
}) {
  const navigate = useNavigate();
  return (
    <div className="flex flex-1 items-center justify-center px-6">
      <div className="flex max-w-md flex-col items-center gap-3 text-center">
        <h1 className="font-medium text-foreground text-lg">Conversation not found</h1>
        <p className="text-muted-foreground text-sm">
          Couldn't load{" "}
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{conversationId}</code>
          : {error.message}
        </p>
        <Button type="button" variant="outline" onClick={() => navigate("/")}>
          Start a new chat
        </Button>
      </div>
    </div>
  );
}