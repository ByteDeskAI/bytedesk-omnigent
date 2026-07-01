"use client";

import { cn } from "@/lib/utils";
import type { HTMLAttributes } from "react";
import { useMemo } from "react";

import { SchemaDisplayContext } from "./context";
import {
  SchemaDisplayDescription,
  SchemaDisplayHeader,
  SchemaDisplayMethod,
  SchemaDisplayPath,
} from "./header";
import { SchemaDisplayParameters } from "./parameters";
import { SchemaDisplayRequest, SchemaDisplayResponse } from "./request-response";
import type { HttpMethod, SchemaParameter, SchemaProperty } from "./types";

export type SchemaDisplayContentProps = HTMLAttributes<HTMLDivElement>;

export const SchemaDisplayContent = ({
  className,
  children,
  ...props
}: SchemaDisplayContentProps) => (
  <div className={cn("divide-y", className)} {...props}>
    {children}
  </div>
);

export type SchemaDisplayProps = HTMLAttributes<HTMLDivElement> & {
  method: HttpMethod;
  path: string;
  description?: string;
  parameters?: SchemaParameter[];
  requestBody?: SchemaProperty[];
  responseBody?: SchemaProperty[];
};

export const SchemaDisplay = ({
  method,
  path,
  description,
  parameters,
  requestBody,
  responseBody,
  className,
  children,
  ...props
}: SchemaDisplayProps) => {
  const contextValue = useMemo(
    () => ({
      description,
      method,
      parameters,
      path,
      requestBody,
      responseBody,
    }),
    [description, method, parameters, path, requestBody, responseBody],
  );

  return (
    <SchemaDisplayContext.Provider value={contextValue}>
      <div className={cn("overflow-hidden rounded-lg border bg-background", className)} {...props}>
        {children ?? (
          <>
            <SchemaDisplayHeader>
              <div className="flex items-center gap-3">
                <SchemaDisplayMethod />
                <SchemaDisplayPath />
              </div>
            </SchemaDisplayHeader>
            {description && <SchemaDisplayDescription />}
            <SchemaDisplayContent>
              {parameters && parameters.length > 0 && <SchemaDisplayParameters />}
              {requestBody && requestBody.length > 0 && <SchemaDisplayRequest />}
              {responseBody && responseBody.length > 0 && <SchemaDisplayResponse />}
            </SchemaDisplayContent>
          </>
        )}
      </div>
    </SchemaDisplayContext.Provider>
  );
};

export type SchemaDisplayBodyProps = HTMLAttributes<HTMLDivElement>;

export const SchemaDisplayBody = ({ className, children, ...props }: SchemaDisplayBodyProps) => (
  <div className={cn("divide-y", className)} {...props}>
    {children}
  </div>
);

export type SchemaDisplayExampleProps = HTMLAttributes<HTMLPreElement>;

export const SchemaDisplayExample = ({
  className,
  children,
  ...props
}: SchemaDisplayExampleProps) => (
  <pre
    className={cn("mx-4 mb-4 overflow-auto rounded-md bg-muted p-4 font-mono text-sm", className)}
    {...props}
  >
    {children}
  </pre>
);