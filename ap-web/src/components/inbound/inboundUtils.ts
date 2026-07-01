export function inboundStatusVariant(status: string): "secondary" | "destructive" | "outline" {
  if (status === "fanned_out" || status === "received") return "secondary";
  if (status === "dead_lettered") return "destructive";
  return "outline";
}

export function inboundTimeLabel(epoch: number): string {
  return new Date(epoch * 1000).toLocaleTimeString();
}