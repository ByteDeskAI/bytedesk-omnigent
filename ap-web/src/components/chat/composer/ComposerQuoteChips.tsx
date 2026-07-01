import { XIcon } from "lucide-react";

export function ComposerQuoteChips({
  quotes,
  onRemoveQuote,
}: {
  quotes: string[];
  onRemoveQuote: (index: number) => void;
}) {
  if (quotes.length === 0) return null;

  return (
    <div className="flex flex-col gap-1.5 px-4 pt-3 pb-0">
      {quotes.map((quote, i) => (
        <div key={i} className="flex items-start gap-2">
          <div className="min-w-0 flex-1 bg-muted/40 rounded-md border-l-2 border-l-primary/60 px-2 py-1.5 text-xs text-muted-foreground">
            <span className="block truncate">
              {quote.length > 120 ? `${quote.slice(0, 120)}…` : quote}
            </span>
          </div>
          <button
            type="button"
            onClick={() => onRemoveQuote(i)}
            className="mt-0.5 shrink-0 rounded-full text-muted-foreground hover:text-foreground"
            aria-label="Remove quote"
          >
            <XIcon className="size-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}