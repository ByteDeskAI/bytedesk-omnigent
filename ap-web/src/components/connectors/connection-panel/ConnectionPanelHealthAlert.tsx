export function ConnectionPanelHealthAlert({
  message,
  clientId,
  requiredScopes,
}: {
  message: string;
  clientId?: string;
  requiredScopes: string[];
}) {
  return (
    <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
      <div className="font-medium">{message}</div>
      {clientId && <div className="mt-1 text-amber-100/80">Client ID: {clientId}</div>}
      {requiredScopes.length > 0 && (
        <div className="mt-1 text-amber-100/80">Required scopes: {requiredScopes.join(", ")}</div>
      )}
    </div>
  );
}