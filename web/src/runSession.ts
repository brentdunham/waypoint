import { api } from "./api/client";
import { streamRun } from "./api/sse";
import type { RunEvent, RunRecord } from "./api/types";

interface Callbacks {
  start: () => void;
  record: (record: RunRecord) => void;
  event: (event: RunEvent) => void;
  error: (error: unknown, stream: boolean) => void;
  settled: () => void | Promise<void>;
  clear: () => void;
}

/** Owns transport lifetime, including a POST that returns after detaching. */
export class RunSession {
  private controller: AbortController | null = null;

  constructor(
    private readonly startRun = api.startRun,
    private readonly follow = streamRun,
    private readonly stop = api.cancel,
  ) {}

  reset() {
    this.controller?.abort();
    this.controller = null;
  }

  async launch(threadId: string, body: Parameters<typeof api.startRun>[1], callbacks: Callbacks) {
    this.reset();
    const controller = new AbortController();
    this.controller = controller;
    const current = () => this.controller === controller && !controller.signal.aborted;
    callbacks.start();
    try {
      const record = await this.startRun(threadId, body);
      if (!current()) return;
      callbacks.record(record);
      await this.follow(record.run_id, {
        signal: controller.signal,
        onEvent: (event) => { if (current()) callbacks.event(event); },
        onError: (error) => { if (current()) callbacks.error(error, true); },
      });
    } catch (error) {
      if (current()) callbacks.error(error, false);
    } finally {
      if (current()) {
        try {
          await callbacks.settled();
        } finally {
          if (current()) callbacks.clear();
        }
      }
    }
  }

  async cancel(runId: string, cancelled: () => void) {
    const controller = this.controller;
    await this.stop(runId);
    if (controller !== null && this.controller === controller) {
      this.reset();
      cancelled();
    }
  }
}
