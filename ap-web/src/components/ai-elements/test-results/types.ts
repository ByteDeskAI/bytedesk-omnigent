export type TestStatusKind = "passed" | "failed" | "skipped" | "running";

export interface TestResultsSummaryData {
  passed: number;
  failed: number;
  skipped: number;
  total: number;
  duration?: number;
}

export interface TestResultsContextType {
  summary?: TestResultsSummaryData;
}

export interface TestSuiteContextType {
  name: string;
  status: TestStatusKind;
}

export interface TestContextType {
  name: string;
  status: TestStatusKind;
  duration?: number;
}