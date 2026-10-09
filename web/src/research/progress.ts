import type { Finding, RunEvent } from "../api/types";

/**
 * What a research run is doing, rebuilt from its event stream.
 *
 * Two kinds of event feed it. `custom` events with `kind: "research"` say what
 * the graph is *starting* (a worker begins searching, the critic begins a
 * review); `node` updates say what *finished*, with the findings. Node updates
 * alone would leave the panel empty for the minute the workers search.
 */

export type Phase =
  | "idle"
  | "planning"
  | "awaiting approval"
  | "searching"
  | "reviewing"
  | "writing"
  | "verifying"
  | "done"
  | "declined"
  | "failed"
  | "cancelled";

export interface Worker {
  question: string;
  round: number;
  status: "searching" | "done";
  sources: number;
  /** False when the worker searched but cited nothing (retrieved-results fallback). */
  cited: boolean;
  confidence: number | null;
  issue: string;
  /** Searcher model: from the run's model map while searching, then from the finding. */
  model: string;
}

export interface Progress {
  phase: Phase;
  round: number;
  workers: Worker[];
  /** Role -> model for this run, from `run.started`. */
  models: Record<string, string>;
}

export const EMPTY_PROGRESS: Progress = { phase: "idle", round: 0, workers: [], models: {} };

const ACTIVE: ReadonlySet<Phase> = new Set([
  "planning",
  "searching",
  "reviewing",
  "writing",
  "verifying",
]);

export function isActive(phase: Phase): boolean {
  return ACTIVE.has(phase);
}

function key(question: string): string {
  return question.trim().toLowerCase().replace(/\?$/, "");
}

function upsert(workers: Worker[], question: string, patch: Partial<Worker>): Worker[] {
  const k = key(question);
  const index = workers.findIndex((w) => key(w.question) === k);
  if (index === -1) {
    const fresh: Worker = {
      question,
      round: 1,
      status: "searching",
      sources: 0,
      cited: true,
      confidence: null,
      issue: "",
      model: "",
      ...patch,
    };
    return [...workers, fresh];
  }
  return workers.map((w, i) => (i === index ? { ...w, ...patch } : w));
}

function findingsOf(update: unknown): Finding[] {
  if (update && typeof update === "object" && "findings" in update) {
    const findings = (update as { findings?: unknown }).findings;
    if (Array.isArray(findings)) return findings as Finding[];
  }
  return [];
}

export function applyEvent(progress: Progress, event: RunEvent): Progress {
  switch (event.type) {
    case "run.started":
      return { ...progress, models: event.data.models ?? {} };
    case "custom": {
      const p = event.data.payload as {
        kind?: string;
        phase?: Phase;
        question?: string;
        round?: number;
      } | null;
      if (!p || p.kind !== "research" || !p.phase) return progress;
      const round = p.round ?? progress.round;
      if (p.phase === "searching" && p.question) {
        return {
          ...progress,
          phase: "searching",
          round,
          workers: upsert(progress.workers, p.question, {
            round,
            status: "searching",
            model: progress.models["searcher"] ?? "",
          }),
        };
      }
      return { ...progress, phase: p.phase, round };
    }
    case "node": {
      const { node, update } = event.data;
      if (node === "declined") return { ...progress, phase: "declined" };
      if (node !== "investigate" && node !== "critique") return progress;
      let workers = progress.workers;
      for (const f of findingsOf(update)) {
        workers = upsert(workers, f.question, {
          round: f.round,
          status: "done",
          sources: f.sources.length,
          cited: f.cited ?? true,
          ...(f.model ? { model: f.model } : {}),
          ...(node === "critique" ? { confidence: f.confidence, issue: f.issue } : {}),
        });
      }
      return { ...progress, workers };
    }
    case "run.interrupted":
      return { ...progress, phase: "awaiting approval" };
    case "run.completed":
      return progress.phase === "idle" || progress.phase === "declined"
        ? progress
        : { ...progress, phase: "done" };
    case "run.failed":
      return { ...progress, phase: "failed" };
    case "run.cancelled":
      return { ...progress, phase: "cancelled" };
    default:
      return progress;
  }
}
