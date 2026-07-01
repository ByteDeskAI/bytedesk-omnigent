"use client";

import { createContext, useContext } from "react";

import type { StackTraceContextValue } from "./types";

export const StackTraceContext = createContext<StackTraceContextValue | null>(null);

export const useStackTrace = () => {
  const context = useContext(StackTraceContext);
  if (!context) {
    throw new Error("StackTrace components must be used within StackTrace");
  }
  return context;
};