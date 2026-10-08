import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import { streamRun } from "../api/sse";
import type { Interrupt, RunEvent, RunResult, RunStatus } from "../api/types";

export interface RunState {
  runId: string | null;
  status: RunStatus | "idle";
  streamedText: string;
  nodes: { node: string; at: number }[];
  interrupts: Interrupt[];
  result: RunResult | null;
  error: string | null;
}

const IDLE: RunState = {
  runId: null,
  status: "idle",
  streamedText: "",
  nodes: [],
  interrupts: [],
  result: null,
  error: null,
};

/**
 * Owns one run at a time: start it, follow its events, cancel it.
 *
 * The abort controller is the important part. Unmounting or starting a new run
 * aborts the fetch, which closes the SSE connection, which makes the server's
 * generator raise `GeneratorExit`, which cancels the graph task. A cancel in
 * the browser really does stop work in the backend.
 */
export function useRun(onSettled?: () => void | Promise<void>) {
  const [state, setState] = useState<RunState>(IDLE);
  const abortRef = useRef<AbortController | null>(null);
  const settled = useRef(onSettled);
  settled.current = onSettled;

  useEffect(() => () => abortRef.current?.abort(), []);

  const consume = useCallback((event: RunEvent) => {
    setState((prev) => {
      switch (event.type) {
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
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setState({ ...IDLE, status: "running" });

      try {
        const record = await api.startRun(threadId, body);
        setState((prev) => ({ ...prev, runId: record.run_id }));
        await streamRun(record.run_id, {
          signal: controller.signal,
          onEvent: consume,
          // Called only once reconnecting has been given up on. The run itself
          // may well still be going server-side; say so rather than "failed".
          onError: (error) =>
            setState((prev) => ({
              ...prev,
              status: prev.status === "running" ? "failed" : prev.status,
              error: `Lost the connection to this run (${String(error)}). It may still be running; reopen the thread to see its result.`,
            })),
        });
      } catch (error) {
        setState((prev) => ({
          ...prev,
          status: "failed",
          error: error instanceof Error ? error.message : String(error),
        }));
      } finally {
        try {
          // Reload the thread so the persisted messages include this reply...
          await settled.current?.();
        } finally {
          // ...then drop the live copy, or the reply renders twice: once from the
          // snapshot and once from the token stream. Clearing only after the
          // reload avoids a frame where the reply disappears. Skip if a newer
          // run has started and owns the stream now.
          if (abortRef.current === controller) {
            setState((prev) => ({ ...prev, streamedText: "" }));
          }
        }
      }
    },
    [consume],
  );

  const cancel = useCallback(async () => {
    if (!state.runId) return;
    try {
      await api.cancel(state.runId);
    } finally {
      abortRef.current?.abort();
      setState((prev) => ({ ...prev, status: "cancelled" }));
    }
  }, [state.runId]);

  const reset = useCallback(() => setState(IDLE), []);

  return { ...state, launch, cancel, reset, busy: state.status === "running" };
}
