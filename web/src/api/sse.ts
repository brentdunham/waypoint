import type { RunEvent } from "./types";
import { TERMINAL_EVENTS } from "./types";
import { headers } from "./client";

/**
 * SSE over `fetch`, not `EventSource`.
 *
 * `EventSource` cannot send an Authorization header and cannot be aborted
 * cleanly, so anything behind a bearer token has to read the stream itself.
 * The trade is that we own reconnection -- which is fine, because we also own
 * `Last-Event-ID`, and the server replays a Redis Stream from exactly that id.
 * A dropped Wi-Fi connection therefore loses nothing and duplicates nothing.
 */
export interface StreamOptions {
  signal?: AbortSignal;
  lastEventId?: string;
  maxRetries?: number;
  onEvent: (event: RunEvent, id: string) => void;
  onError?: (error: unknown) => void;
}

const BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";

export async function streamRun(runId: string, options: StreamOptions): Promise<void> {
  const { signal, onEvent, onError, maxRetries = 5 } = options;
  let lastId = options.lastEventId ?? "0";
  let attempt = 0;

  while (!signal?.aborted) {
    let failure: unknown;
    try {
      const response = await fetch(`${BASE}/runs/${runId}/events`, {
        headers: headers({ Accept: "text/event-stream", "Last-Event-ID": lastId }),
        ...(signal ? { signal } : {}),
      });
      if (!response.ok || !response.body) {
        const error = new Error(`stream failed with ${response.status}`);
        // A 4xx other than timeout/rate-limit will not get better by retrying.
        if (response.status >= 400 && response.status < 500 && ![408, 429].includes(response.status)) {
          onError?.(error);
          return;
        }
        throw error;
      }

      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;

        // Frames are separated by a blank line. Anything after the last
        // separator is a partial frame and stays in the buffer.
        let split: number;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const parsed = parseFrame(frame);
          if (!parsed) continue;
          attempt = 0; // progress: the next drop starts a fresh retry budget
          lastId = parsed.id || lastId;
          onEvent(parsed.event, lastId);
          if (TERMINAL_EVENTS.has(parsed.event.type)) return;
        }
      }
      if (signal?.aborted) return;
      // The server ended the response without a terminal event. The run is
      // still going (the server sends one when it ends), so resume from lastId.
      failure = new Error("stream closed before the run finished");
    } catch (error) {
      if (signal?.aborted || (error as Error)?.name === "AbortError") return;
      failure = error;
    }
    // Drops are routine and recovered silently; only giving up is an error.
    if (++attempt > maxRetries) {
      onError?.(failure);
      return;
    }
    await sleep(Math.min(500 * 2 ** (attempt - 1), 8_000));
  }
}

function parseFrame(frame: string): { id: string; event: RunEvent } | null {
  let id = "";
  let type = "message";
  const dataLines: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue; // comment / keep-alive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "id") id = value;
    else if (field === "event") type = value;
    else if (field === "data") dataLines.push(value);
  }
  if (dataLines.length === 0) return null;

  try {
    const data = JSON.parse(dataLines.join("\n")) as unknown;
    return { id, event: { type, data } as RunEvent };
  } catch {
    return null;
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
