export function EmptyOutputState({ state }: { state: "output-error" | "cancelled" | "no-output" }) {
  let message: string;
  if (state === "cancelled") {
    message = "Tool was cancelled before output arrived.";
  } else if (state === "no-output") {
    message = "No output was recorded for this tool call.";
  } else {
    message = "Tool did not return output before the response failed.";
  }
  return (
    <div className="rounded-md border border-dashed bg-muted/30 px-3 py-2 text-muted-foreground text-sm">
      {message}
    </div>
  );
}