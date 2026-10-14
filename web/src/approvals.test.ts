import { describe, expect, it } from "vitest";

import { formatArgs, isToolApproval, pendingCalls, toolDecision } from "./approvals";

const CALLS = [
  { id: "a", tool: "notes_add", server: "notes", remote_tool: "add", description: "Add a note.", args: { text: "hi" }, trusted: true },
  { id: "b", tool: "notes_delete", server: "notes", remote_tool: "delete", description: "Delete.", args: { id: 3 }, trusted: true },
];

describe("tool approval", () => {
  it("recognises its interrupt and reads the calls", () => {
    expect(isToolApproval({ kind: "approve_tools", calls: CALLS })).toBe(true);
    expect(isToolApproval({ kind: "approve_plan" })).toBe(false);
    expect(pendingCalls({ kind: "approve_tools", calls: CALLS })).toHaveLength(2);
    expect(pendingCalls({ kind: "approve_tools" })).toEqual([]);
  });

  it("answers every call, declining what was not allowed", () => {
    expect(toolDecision(CALLS, { a: true })).toEqual({ tools: { a: true, b: false } });
  });

  it("shows arguments one per line", () => {
    expect(formatArgs({ text: "hi", id: 3, tags: ["x"] })).toBe('text: hi\nid: 3\ntags: ["x"]');
    expect(formatArgs({})).toBe("(no arguments)");
  });
});
