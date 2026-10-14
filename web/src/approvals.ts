import type { InterruptPayload } from "./api/types";

/** One call `approve_tools` asks about (docs/design/tools.md, T4). */
export interface PendingCall {
  id: string;
  tool: string;
  server: string;
  remote_tool: string;
  description: string;
  args: Record<string, unknown>;
  trusted: boolean;
}

export function isToolApproval(value: InterruptPayload): boolean {
  return value.kind === "approve_tools";
}

export function pendingCalls(value: InterruptPayload): PendingCall[] {
  return Array.isArray(value.calls) ? (value.calls as PendingCall[]) : [];
}

/** The resume payload: every call answered, so nothing is declined by omission. */
export function toolDecision(calls: PendingCall[], allowed: Record<string, boolean>) {
  return { tools: Object.fromEntries(calls.map((c) => [c.id, allowed[c.id] === true])) };
}

/** Arguments as the person should read them: one `key: value` per line. */
export function formatArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args);
  if (entries.length === 0) return "(no arguments)";
  return entries
    .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join("\n");
}
