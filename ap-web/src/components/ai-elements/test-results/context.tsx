"use client";

import { createContext } from "react";
import type {
  TestContextType,
  TestResultsContextType,
  TestSuiteContextType,
} from "./types";

export const TestResultsContext = createContext<TestResultsContextType>({});

export const TestSuiteContext = createContext<TestSuiteContextType>({
  name: "",
  status: "passed",
});

export const TestContext = createContext<TestContextType>({
  name: "",
  status: "passed",
});