"use client";

import { createContext } from "react";

export interface EnvironmentVariablesContextType {
  showValues: boolean;
  setShowValues: (show: boolean) => void;
}

// oxlint-disable-next-line eslint(no-empty-function)
const noop = () => {};

export const EnvironmentVariablesContext = createContext<EnvironmentVariablesContextType>({
  setShowValues: noop,
  showValues: false,
});

export interface EnvironmentVariableContextType {
  name: string;
  value: string;
}

export const EnvironmentVariableContext = createContext<EnvironmentVariableContextType>({
  name: "",
  value: "",
});