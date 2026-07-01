"use client";

import { createContext, useContext } from "react";

export const OpenInContext = createContext<{ query: string } | undefined>(undefined);

export const useOpenInContext = () => {
  const context = useContext(OpenInContext);
  if (!context) {
    throw new Error("OpenIn components must be used within an OpenIn provider");
  }
  return context;
};