import { describe, expect, it } from "vitest";

import type { RunEvent } from "../api/types";
import { applyEvent, EMPTY_PROGRESS, type Progress } from "./progress";

const custom = (payload: Record<string, unknown>): RunEvent => ({
  type: "custom",
  data: { payload: { kind: "research", ...payload } },
});

const finding = (question: string, extra: Record<string, unknown> = {}) => ({
  question,
  answer: "a",
  sources: [{ url: "https://x", title: "x" }],
  confidence: null,
  issue: "",
  round: 1,
  ...extra,
});

function run(events: RunEvent[]): Progress {
  return events.reduce(applyEvent, EMPTY_PROGRESS);
}

describe("research progress", () => {
  it("shows workers while they search, before any of them finishes", () => {
    const p = run([
      custom({ phase: "searching", question: "What is X?", round: 1 }),
      custom({ phase: "searching", question: "Why X?", round: 1 }),
    ]);
    expect(p.phase).toBe("searching");
    expect(p.workers.map((w) => w.status)).toEqual(["searching", "searching"]);
  });

  it("marks a worker done from its node update, then scores it from the critic", () => {
    const p = run([
      custom({ phase: "searching", question: "What is X?", round: 1 }),
      { type: "node", data: { node: "investigate", update: { findings: [finding("What is X?", { cited: false })] } } },
      custom({ phase: "reviewing", round: 1 }),
      {
        type: "node",
        data: {
          node: "critique",
          update: { findings: [finding("What is X?", { cited: false, confidence: 0.3, issue: "thin" })] },
        },
      },
    ]);
    expect(p.phase).toBe("reviewing");
    expect(p.workers).toEqual([
      { question: "What is X?", round: 1, status: "done", sources: 1, cited: false, confidence: 0.3, issue: "thin" },
    ]);
  });

  it("adds follow-up workers in a later round and finishes on completion", () => {
    const p = run([
      custom({ phase: "searching", question: "What is X?", round: 1 }),
      custom({ phase: "searching", question: "Is Y still true?", round: 2 }),
      custom({ phase: "writing" }),
      { type: "run.completed", data: { result: {} } },
    ]);
    expect(p.round).toBe(2);
    expect(p.workers.map((w) => w.round)).toEqual([1, 2]);
    expect(p.phase).toBe("done");
  });

  it("waits for approval, and records a declined plan", () => {
    const paused = run([
      custom({ phase: "planning" }),
      { type: "run.interrupted", data: { interrupts: [], next: ["approve"] } },
    ]);
    expect(paused.phase).toBe("awaiting approval");
    const declined = run([
      { type: "node", data: { node: "declined", update: {} } },
      { type: "run.completed", data: { result: {} } },
    ]);
    expect(declined.phase).toBe("declined");
  });

  it("ignores other agents' custom events", () => {
    expect(applyEvent(EMPTY_PROGRESS, { type: "custom", data: { payload: { kind: "other" } } })).toBe(
      EMPTY_PROGRESS,
    );
  });
});
