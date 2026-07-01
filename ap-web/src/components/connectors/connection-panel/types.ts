import type { ConnectorConnection, ConnectorManifest } from "@/lib/connectorsApi";
import type { ConnectorPresentation } from "@/lib/connectorPresentation";

export interface ConnectionPanelProps {
  connection: ConnectorConnection;
  provider: ConnectorManifest;
  presentation: ConnectorPresentation;
}