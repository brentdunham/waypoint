import { describe, expect, it } from "vitest";

import type { RunUsage } from "./api/types";
import { formatCost, formatUsage, usageBreakdown } from "./usage";

const USAGE: RunUsage = {
  cost_usd: 0.2534,
  input_tokens: 123_456,
  output_tokens: 7_890,
  searches: 12,
  roles: {
    searcher: { model: "claude-haiku-5-5", calls: 6, searches: 12, cost_usd: 0.13 },
    writer: { model: "claude-sonnet-5", calls: 1, cost_usd: 0.1234 },
    rerank: { model: "rerank-3", tokens: 4000, cost_usd: 0.0002 },
  },
};

describe("usage line", () => {
  it("shows cost, tokens, searches and duration", () => {
    expect(formatUsage(USAGE, 83_400)).toBe("$0.253 · 123,456 in / 7,890 out · 12 searches · 83s");
  });

  it("keeps small costs readable and says when the cost is unknown", () => {
    expect(formatCost(0.00042)).toBe("$0.0004");
    expect(formatCost(0)).toBe("$0");
    expect(formatCost(null)).toBe("cost unknown");
  });

  it("leaves out searches when there were none, and duration when unknown", () => {
    const chat = { ...USAGE, cost_usd: 0.0012, searches: 0 };
    expect(formatUsage(chat, null)).toBe("$0.0012 · 123,456 in / 7,890 out");
    expect(formatUsage(chat, 4_200)).toBe("$0.0012 · 123,456 in / 7,890 out · 4.2s");
  });

  it("breaks the cost down by role, most expensive first", () => {
    expect(usageBreakdown(USAGE).split("\n")).toEqual([
      "searcher (claude-haiku-5-5): $0.130",
      "writer (claude-sonnet-5): $0.123",
      "rerank (rerank-3): $0.0002",
    ]);
  });
});
