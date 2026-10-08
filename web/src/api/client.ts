import type {
  AgentSpec,
  Problem,
  RunRecord,
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

  cancel: (runId: string) =>
    request<RunRecord>(`/runs/${runId}/cancel`, { method: "POST" }),

  memories: (q?: string) =>
    request<{ memories: { key: string; value: { fact?: string }; created_at: string }[] }>(
      `/memories${q ? `?q=${encodeURIComponent(q)}` : ""}`,
    ).then((r) => r.memories),

  forget: (key: string) => request<void>(`/memories/${key}`, { method: "DELETE" }),

  ready: () => request<{ status: string; checks: Record<string, string> }>("/readyz"),
};
