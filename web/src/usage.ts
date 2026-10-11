import type { RunUsage } from "./api/types";

const count = new Intl.NumberFormat("en-US");

/** "$0.0123" with enough digits for a cheap run to show; "cost unknown" when unpriced. */
export function formatCost(usd: number | null): string {
  if (usd === null) return "cost unknown";
  if (usd === 0) return "$0";
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(3)}`;
}

/** The line under a finished reply: cost, tokens in and out, searches, duration. */
export function formatUsage(usage: RunUsage, durationMs: number | null): string {
  const parts = [
    formatCost(usage.cost_usd),
    `${count.format(usage.input_tokens)} in / ${count.format(usage.output_tokens)} out`,
  ];
  if (usage.searches > 0) {
    parts.push(`${usage.searches} ${usage.searches === 1 ? "search" : "searches"}`);
  }
  if (durationMs !== null) parts.push(`${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)}s`);
  return parts.join(" · ");
}

/** Hover text: what each role cost, most expensive first. */
export function usageBreakdown(usage: RunUsage): string {
  return Object.entries(usage.roles)
    .sort(([, a], [, b]) => (b.cost_usd ?? Infinity) - (a.cost_usd ?? Infinity))
    .map(([role, row]) => `${role} (${row.model}): ${formatCost(row.cost_usd)}`)
    .join("\n");
}
