import { Link } from "@/lib/routing";

export function ConnectorBreadcrumbs({ providerName }: { providerName?: string }) {
  return (
    <nav aria-label="Breadcrumb" className="mb-4 text-xs text-muted-foreground">
      <ol className="flex flex-wrap items-center gap-1.5">
        <li>
          {providerName ? (
            <Link to="/connectors" className="hover:text-foreground hover:underline">
              Connectors
            </Link>
          ) : (
            <span className="text-foreground">Connectors</span>
          )}
        </li>
        {providerName && (
          <>
            <li aria-hidden="true">/</li>
            <li className="text-foreground">{providerName}</li>
          </>
        )}
      </ol>
    </nav>
  );
}