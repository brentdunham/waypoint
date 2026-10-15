import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import { RunSession } from "../runSession";
import { TERMINAL_EVENTS } from "../api/types";
import type { Interrupt, RunEvent, RunResult, RunStatus, RunUsage } from "../api/types";
import { applyEvent, EMPTY_PROGRESS, type Progress } from "../research/progress";

export interface RunState {
  runId: string | null;
  status: RunStatus | "idle";
  streamedText: string;
  nodes: { node: string; at: number }[];
  interrupts: Interrupt[];
  result: RunResult | null;
  error: string | null;
  /** Research runs only: workers and phase, rebuilt from the stream. */
  progress: Progress;
  /** What the run cost, from its terminal event. */
  usage: RunUsage | null;
  /** Milliseconds from starting the run to its terminal event. */
  durationMs: number | null;
  startedAt: number | null;
  recoveryNotice: string | null;
}

const IDLE: RunState = {
  runId: null,
  status: "idle",
  streamedText: "",
  nodes: [],
  interrupts: [],
  result: null,
  error: null,
  progress: EMPTY_PROGRESS,
  usage: null,
  durationMs: null,
  startedAt: null,
  recoveryNotice: null,
};

/**
 * Owns one run at a time: start it, follow its events, cancel it.
 *
 * Unmounting or starting a new run aborts the fetch, which closes the SSE
 * connection. That stops *following* the run, not the run: it keeps going
 * server-side and can be reattached. Stopping it is `cancel`, which asks the
 * server to cancel the task.
 */
export function useRun(onSettled?: () => void | Promise<void>) {
  const [state, setState] = useState<RunState>(IDLE);
  const session = useRef(new RunSession());
  const settled = useRef(onSettled);
  settled.current = onSettled;

  useEffect(() => () => session.current.reset(), []);

  const consume = useCallback((event: RunEvent) => {
    setState((current) => {
      let prev = { ...current, progress: applyEvent(current.progress, event) };
      if (TERMINAL_EVENTS.has(event.type)) {
        const data = event.data as { usage?: RunUsage | null };
        prev = {
          ...prev,
          usage: data.usage ?? null,
          durationMs: prev.startedAt === null ? null : Date.now() - prev.startedAt,
        };
      }
      switch (event.type) {
        case "run.reclaimed":
          return {
            ...prev,
            streamedText: "",
            progress: event.data.from_checkpoint ? prev.progress : EMPTY_PROGRESS,
            recoveryNotice: `Resumed on another worker (attempt ${event.data.attempt}).`,
          };
        case "run.started":
          return { ...prev, status: "running", runId: event.data.run_id };
        case "token":
          return { ...prev, streamedText: prev.streamedText + event.data.text };
        case "node":
          return { ...prev, nodes: [...prev.nodes, { node: event.data.node, at: Date.now() }] };
        case "run.interrupted":
          return { ...prev, status: "interrupted", interrupts: event.data.interrupts };
        case "run.completed":
          return { ...prev, status: "completed", result: event.data.result };
        case "run.failed":
          return { ...prev, status: "failed", error: event.data.error };
        case "run.cancelled":
          return { ...prev, status: "cancelled" };
        default:
          return prev;
      }
    });
  }, []);

  const launch = useCallback(
    async (
      threadId: string,
      body: Parameters<typeof api.startRun>[1],
    ): Promise<void> => {
      await session.current.launch(threadId, body, {
        start: () => setState({ ...IDLE, status: "running", startedAt: Date.now() }),
        record: (record) => setState((prev) => ({ ...prev, runId: record.run_id })),
        event: consume,
        error: (error, stream) => setState((prev) => ({
          ...prev,
          status: "failed",
          error: stream
            ? `Lost the connection to this run (${String(error)}). It may still be running; reopen the thread to see its result.`
            : error instanceof Error ? error.message : String(error),
        })),
        settled: () => settled.current?.(),
        clear: () => setState((prev) => ({ ...prev, streamedText: "" })),
      });
    },
    [consume],
  );

  const cancel = useCallback(async () => {
    if (!state.runId) return;
    await session.current.cancel(state.runId, () => {
      setState((prev) => ({ ...prev, status: "cancelled" }));
      void settled.current?.();
    });
  }, [state.runId]);

  const reset = useCallback(() => {
    session.current.reset();
    setState(IDLE);
  }, []);

  return { ...state, launch, cancel, reset, busy: state.status === "running" };
}
