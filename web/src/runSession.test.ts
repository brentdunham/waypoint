import { describe, expect, it, vi } from "vitest";
import type { RunRecord } from "./api/types";
import type { StreamOptions } from "./api/sse";
import { RunSession } from "./runSession";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const record = (id: string) => ({ run_id: id }) as RunRecord;
const callbacks = () => ({
  start: vi.fn(), record: vi.fn(), event: vi.fn(), error: vi.fn(),
  settled: vi.fn(), clear: vi.fn(),
});

describe("run session ownership", () => {
  it("ignores a POST that returns after switching threads", async () => {
    const post = deferred<RunRecord>();
    const follow = vi.fn(async () => {});
    const session = new RunSession(() => post.promise, follow);
    const cb = callbacks();
    const running = session.launch("old", { message: "hello" }, cb);
    session.reset();
    post.resolve(record("old-run"));
    await running;
    expect(follow).not.toHaveBeenCalled();
    expect(cb.record).not.toHaveBeenCalled();
    expect(cb.settled).not.toHaveBeenCalled();
  });

  it("aborts the old stream and ignores late tokens and errors", async () => {
    const ended = deferred<void>();
    let stream!: StreamOptions;
    const session = new RunSession(async () => record("old"), async (_id, options) => {
      stream = options;
      await ended.promise;
    });
    const cb = callbacks();
    const running = session.launch("old", {}, cb);
    await Promise.resolve();
    session.reset();
    expect(stream.signal?.aborted).toBe(true);
    stream.onEvent({ type: "token", data: { text: "late", node: "model" } }, "1-0");
    stream.onError?.(new Error("late error"));
    ended.resolve();
    await running;
    expect(cb.event).not.toHaveBeenCalled();
    expect(cb.error).not.toHaveBeenCalled();
    expect(cb.clear).not.toHaveBeenCalled();
  });

  it("does not clear a newer run when the previous refresh finishes", async () => {
    const refresh = deferred<void>();
    const old = callbacks();
    old.settled.mockImplementation(() => refresh.promise);
    const session = new RunSession(async (id) => record(id), async () => {});
    const first = session.launch("first", {}, old);
    await Promise.resolve(); await Promise.resolve();
    const next = callbacks();
    await session.launch("next", {}, next);
    refresh.resolve();
    await first;
    expect(old.clear).not.toHaveBeenCalled();
    expect(next.clear).toHaveBeenCalledOnce();
  });

  it("ignores a failed old POST and a late cancel response", async () => {
    const post = deferred<RunRecord>();
    const cancelled = deferred<RunRecord>();
    const session = new RunSession(() => post.promise, async () => {}, () => cancelled.promise);
    const old = callbacks();
    const first = session.launch("first", {}, old);
    const onCancel = vi.fn();
    const cancel = session.cancel("first", onCancel);
    session.reset();
    post.reject(new Error("old failure"));
    cancelled.resolve(record("first"));
    await Promise.all([first, cancel]);
    expect(old.error).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });
});
