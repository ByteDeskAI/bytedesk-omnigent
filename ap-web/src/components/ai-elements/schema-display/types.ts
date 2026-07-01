export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface SchemaParameter {
  name: string;
  type: string;
  required?: boolean;
  description?: string;
  location?: "path" | "query" | "header";
}

export interface SchemaProperty {
  name: string;
  type: string;
  required?: boolean;
  description?: string;
  properties?: SchemaProperty[];
  items?: SchemaProperty;
}

export interface SchemaDisplayContextType {
  method: HttpMethod;
  path: string;
  description?: string;
  parameters?: SchemaParameter[];
  requestBody?: SchemaProperty[];
  responseBody?: SchemaProperty[];
}