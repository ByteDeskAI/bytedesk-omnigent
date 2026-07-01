import type { PolicyRegistryEntry } from "@/hooks/usePolicies";

export type PolicyParamProperty = {
  type?: string;
  description?: string;
  default?: unknown;
  enum?: string[];
  items?: { type?: string; enum?: string[] };
  uniqueItems?: boolean;
};

export type PolicyParamsSchema = {
  properties?: Record<string, PolicyParamProperty>;
  required?: string[];
};

export type PolicyDialogPickerProps = {
  registry: PolicyRegistryEntry[];
  appliedHandlers: Set<string>;
  filter: string;
  onFilterChange: (value: string) => void;
  onSelect: (handler: string) => void;
  emptyAllMessage: string;
};

export type PolicySelectedEntryProps = {
  entry: PolicyRegistryEntry;
  onClear: () => void;
};

export type PolicyParamFieldsProps = {
  paramKeys: string[];
  properties: Record<string, PolicyParamProperty>;
  factoryParams: Record<string, string>;
  onFactoryParamsChange: (updater: (prev: Record<string, string>) => Record<string, string>) => void;
};