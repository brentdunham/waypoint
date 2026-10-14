import type { RunTrace, TraceStep } from "./api/types";
import { formatCost } from "./usage";

const count = new Intl.NumberFormat("en-US");

/** The graph's node names, as a reviewer reads them. */
const NODE_LABELS: Record<string, string> = {
  plan: "Plan",
  approve: "Approval",
  investigate: "Research",
  critique: "Review",
  synthesize: "Write",
  verify: "Check citations",
  declined: "Declined",
  recall_memories: "Recall memories",
  model: "Reply",
  tools: "Tools",
};

export interface Row {
  step: TraceStep;
  label: string;
  children: TraceStep[];
  costUsd: number;
}

export interface Summary {
  durationMs: number;
  costUsd: number | null;
  modelCalls: number;
  documentSearches: number;
  webSearches: number;
}

export function nodeLabel(step: TraceStep): string {
  const node = step.node ?? step.name.replace(/^node /, "");
  return NODE_LABELS[node] ?? node.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** One row per graph node, in start order, with what ran inside it. Parallel
 *  steps of the same node (research workers) are numbered. */
export function rows(trace: RunTrace): Row[] {
  const nodes = trace.steps.filter((s) => s.kind === "node");
  const byNode = new Map<string, TraceStep[]>(nodes.map((n) => [n.id, []]));
  for (const step of trace.steps) {
    if (step.kind !== "node" && step.parent && byNode.has(step.parent)) {
      byNode.get(step.parent)!.push(step);
    }
  }
  const total: Record<string, number> = {};
  for (const n of nodes) total[nodeLabel(n)] = (total[nodeLabel(n)] ?? 0) + 1;
  const seen: Record<string, number> = {};
  return nodes.map((step) => {
    const base = nodeLabel(step);
    seen[base] = (seen[base] ?? 0) + 1;
    const children = byNode.get(step.id) ?? [];
    return {
      step,
      label: total[base]! > 1 ? `${base} ${seen[base]}` : base,
      children,
      costUsd: children.reduce((sum, c) => sum + (c.cost_usd ?? 0), 0),
    };
  });
}

export function summarize(trace: RunTrace): Summary {
  const models = trace.steps.filter((s) => s.kind === "model");
  return {
    durationMs: trace.duration_ms ?? 0,
    costUsd: trace.cost_usd ?? null,
    modelCalls: models.length,
    documentSearches: trace.steps.filter((s) => s.kind === "retrieval").length,
    webSearches: models.reduce((sum, s) => sum + (s.searches ?? 0), 0),
  };
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

export function summaryLine(summary: Summary): string {
  const parts = [formatDuration(summary.durationMs)];
  if (summary.costUsd !== null) parts.push(formatCost(summary.costUsd));
  parts.push(`${summary.modelCalls} model ${summary.modelCalls === 1 ? "call" : "calls"}`);
  if (summary.webSearches) parts.push(`${summary.webSearches} web ${summary.webSearches === 1 ? "search" : "searches"}`);
  if (summary.documentSearches) {
    parts.push(`${summary.documentSearches} document ${summary.documentSearches === 1 ? "search" : "searches"}`);
  }
  return parts.join(" · ");
}

/** What one step inside a node did, in a line. */
export function describe(step: TraceStep): string {
  const parts: string[] = [];
  if (step.kind === "model") {
    parts.push(`${step.role ?? "model"} · ${step.model ?? "unknown model"}`);
    parts.push(formatDuration(step.duration_ms));
    if (step.input_tokens !== undefined && step.input_tokens !== null) {
      parts.push(`${count.format(step.input_tokens)} in / ${count.format(step.output_tokens ?? 0)} out`);
    }
    if (step.searches) parts.push(`${step.searches} web ${step.searches === 1 ? "search" : "searches"}`);
    if (step.cost_usd !== undefined && step.cost_usd !== null) parts.push(formatCost(step.cost_usd));
  } else if (step.kind === "retrieval") {
    parts.push("Document search");
    parts.push(formatDuration(step.duration_ms));
    if (step.hits !== undefined && step.hits !== null) {
      parts.push(`${step.hits} ${step.hits === 1 ? "passage" : "passages"}${step.mode ? ` (${step.mode})` : ""}`);
    }
  } else {
    parts.push(`Tool · ${step.tool ?? step.name.replace(/^execute_tool /, "")}`);
    if (step.server) parts.push(`${step.server} server`);
    parts.push(formatDuration(step.duration_ms));
  }
  if (step.error) parts.push("failed");
  return parts.join(" · ");
}
