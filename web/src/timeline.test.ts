import { describe, expect, it } from "vitest";

import type { RunTrace, TraceStep } from "./api/types";
import { describe as describeStep, formatDuration, rows, summarize, summaryLine } from "./timeline";

function step(fields: Partial<TraceStep> & Pick<TraceStep, "id" | "kind" | "name">): TraceStep {
  return { parent: null, start_ms: 0, duration_ms: 1000, error: false, ...fields };
}

const TRACE: RunTrace = {
  status: "ready",
  run_id: "r",
  duration_ms: 115_230,
  cost_usd: 0.310202,
  steps: [
    step({ id: "a", kind: "node", name: "node approve", node: "approve", duration_ms: 2 }),
    step({ id: "w1", kind: "node", name: "node investigate", node: "investigate", start_ms: 10, duration_ms: 12_000 }),
    step({ id: "w2", kind: "node", name: "node investigate", node: "investigate", start_ms: 12, duration_ms: 14_000 }),
    step({ id: "d1", kind: "retrieval", name: "rag.retrieve", parent: "w1", hits: 3, mode: "hybrid", duration_ms: 420 }),
    step({
      id: "m1", kind: "model", name: "chat claude-haiku-5-5", parent: "w1", role: "searcher",
      model: "claude-haiku-5-5", input_tokens: 24_000, output_tokens: 1_900, searches: 2, cost_usd: 0.0234,
    }),
    step({
      id: "m2", kind: "model", name: "chat claude-haiku-5-5", parent: "w2", role: "searcher",
      model: "claude-haiku-5-5", input_tokens: 20_000, output_tokens: 1_500, searches: 1, cost_usd: 0.0128,
    }),
    step({ id: "s", kind: "node", name: "node synthesize", node: "synthesize", start_ms: 90_000, duration_ms: 13_700 }),
    step({ id: "t", kind: "tool", name: "execute_tool remember", parent: "s", tool: "remember", duration_ms: 30 }),
  ],
};

describe("run timeline", () => {
  it("makes a row per node, numbers parallel workers, and nests what ran inside", () => {
    const out = rows(TRACE);
    expect(out.map((r) => r.label)).toEqual(["Approval", "Research 1", "Research 2", "Write"]);
    expect(out[1]!.children.map((c) => c.id)).toEqual(["d1", "m1"]);
    expect(out[1]!.costUsd).toBeCloseTo(0.0234);
  });

  it("sums the run in one line", () => {
    const summary = summarize(TRACE);
    expect(summary).toMatchObject({ modelCalls: 2, webSearches: 3, documentSearches: 1 });
    expect(summaryLine(summary)).toBe(
      "1m 55s · $0.310 · 2 model calls · 3 web searches · 1 document search",
    );
  });

  it("describes each kind of step in words", () => {
    const [, worker, , write] = rows(TRACE);
    expect(describeStep(worker!.children[1]!)).toBe(
      "searcher · claude-haiku-5-5 · 1.0s · 24,000 in / 1,900 out · 2 web searches · $0.023",
    );
    expect(describeStep(worker!.children[0]!)).toBe("Document search · 420ms · 3 passages (hybrid)");
    expect(describeStep(write!.children[0]!)).toBe("Tool · remember · 30ms");
  });

  it("formats durations at every scale", () => {
    expect([formatDuration(850), formatDuration(4_200), formatDuration(43_539), formatDuration(115_230)]).toEqual([
      "850ms",
      "4.2s",
      "44s",
      "1m 55s",
    ]);
  });
});
