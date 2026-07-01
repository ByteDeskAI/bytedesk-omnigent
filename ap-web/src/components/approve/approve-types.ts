export interface ElicitationData {
  status: "pending" | "resolved";
  message?: string;
  phase?: string;
  policy_name?: string;
  content_preview?: string;
}

export type ApprovePageState =
  | { kind: "loading" }
  | { kind: "pending"; data: ElicitationData }
  | { kind: "resolved" }
  | { kind: "submitted"; action: "accept" | "decline" }
  | { kind: "error"; message: string };