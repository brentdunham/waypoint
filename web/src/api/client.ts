import type {
  AgentSpec,
  Corpus,
  Problem,
  RunRecord,
  RunTrace,
  Snapshot,
  Thread,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly problem: Problem,
  ) {
    super(problem.detail || problem.title);
    this.name = "ApiError";
  }
}

/** The API key lives in localStorage for this demo. In a real deployment put a
 *  session cookie in front of this and never let a long-lived key near the DOM. */
const KEY = "waypoint.apiKey";

export const auth = {
  get(): string {
    // "agents.apiKey" is the key's name from before the rename to Waypoint.
    return localStorage.getItem(KEY) ?? localStorage.getItem("agents.apiKey") ?? "";
  },
  set(key: string): void {
    localStorage.setItem(KEY, key);
    localStorage.removeItem("agents.apiKey");
  },
};

/**
 * A personal demo link carries its key in the URL fragment: `/#key=ak_…`.
 * Browsers never send the fragment to the server, so the key stays out of
 * access logs and Referer headers. Returns the key and what should remain of
 * the fragment, or null when there is no key.
 */
export function keyFromHash(hash: string): { key: string; rest: string } | null {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const key = params.get("key")?.trim() ?? "";
  if (!/^ak_[A-Za-z0-9]+\.[A-Za-z0-9_-]+$/.test(key)) return null;
  params.delete("key");
  return { key, rest: params.toString() };
}

/** Store a key from a personal link, then remove it from the address bar so it
 *  is not bookmarked, shared or left in history. Call before the first render. */
export function adoptKeyFromUrl(): void {
  const found = keyFromHash(window.location.hash);
  if (!found) return;
  auth.set(found.key);
  const { pathname, search } = window.location;
  window.history.replaceState(null, "", pathname + search + (found.rest ? `#${found.rest}` : ""));
}

export function headers(extra: HeadersInit = {}): HeadersInit {
  const key = auth.get();
  return {
    "Content-Type": "application/json",
    ...(key ? { Authorization: `Bearer ${key}` } : {}),
    ...extra,
  };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: headers(init.headers ?? {}),
  });
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => ({}) as Problem);
  if (!response.ok) throw new ApiError(response.status, body as Problem);
  return body as T;
}

export const api = {
  agents: () => request<{ agents: AgentSpec[] }>("/agents").then((r) => r.agents),

  listThreads: () =>
    request<{ threads: Thread[] }>("/threads").then((r) => r.threads),

  createThread: (agent: string, title?: string) =>
    request<Thread>("/threads", {
      method: "POST",
      body: JSON.stringify({ agent, title: title ?? null }),
    }),

  deleteThread: (id: string) =>
    request<void>(`/threads/${id}`, { method: "DELETE" }),

  state: (id: string, checkpointId?: string) =>
    request<Snapshot>(
      `/threads/${id}/state${checkpointId ? `?checkpoint_id=${encodeURIComponent(checkpointId)}` : ""}`,
    ),

  history: (id: string, limit = 25) =>
    request<{ checkpoints: Snapshot[] }>(
      `/threads/${id}/history?limit=${limit}`,
    ).then((r) => r.checkpoints),

  forkState: (id: string, values: Record<string, unknown>, checkpointId: string, asNode?: string) =>
    request<{ checkpoint_id: string; thread_id: string }>(`/threads/${id}/state`, {
      method: "POST",
      body: JSON.stringify({ values, checkpoint_id: checkpointId, as_node: asNode ?? null }),
    }),

  startRun: (
    id: string,
    body: {
      message?: string;
      input?: Record<string, unknown>;
      resume?: Record<string, unknown> | string;
      checkpoint_id?: string;
      /** Role -> model; "default" covers unnamed roles. Must be in MODELS_ALLOWED. */
      models?: Record<string, string>;
    },
  ) => request<RunRecord>(`/threads/${id}/runs`, { method: "POST", body: JSON.stringify(body) }),

  run: (runId: string) => request<RunRecord>(`/runs/${runId}`),

  /** The run's timeline. 404 when the server has run timelines off. */
  runTrace: (runId: string) => request<RunTrace>(`/runs/${runId}/trace`),

  cancel: (runId: string) =>
    request<RunRecord>(`/runs/${runId}/cancel`, { method: "POST" }),

  memories: (q?: string) =>
    request<{ memories: { key: string; value: { fact?: string }; created_at: string }[] }>(
      `/memories${q ? `?q=${encodeURIComponent(q)}` : ""}`,
    ).then((r) => r.memories),

  forget: (key: string) => request<void>(`/memories/${key}`, { method: "DELETE" }),

  ready: () => request<{ status: string; checks: Record<string, string> }>("/readyz"),

  /** Corpora the caller can search. Empty when document retrieval is off (503). */
  corpora: () =>
    request<{ corpora: Corpus[] }>("/corpora").then(
      (r) => r.corpora,
      (error: unknown) => {
        if (error instanceof ApiError && error.status === 503) return [];
        throw error;
      },
    ),
};
