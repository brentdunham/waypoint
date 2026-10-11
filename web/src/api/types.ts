// Mirrors waypoint/schemas.py and waypoint/serialization.py. Keep the two in step; the
// backend is the source of truth and these types are the contract you break
// loudly rather than quietly.

export interface AgentSpec {
  name: string;
  title: string;
  description: string;
  input_key: string;
  /** Model call sites; a run may choose a model per role (`models`). */
  model_roles: string[];
  supports_interrupts: boolean;
  example: Record<string, unknown>;
}

export interface Thread {
  thread_id: string;
  tenant_id: string;
  user_id: string;
  agent: string;
  title: string | null;
  metadata: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export type RunStatus =
  | "running"
  | "completed"
  | "interrupted"
  | "cancelled"
  | "failed";

export interface RunRecord {
  run_id: string;
  thread_id: string;
  agent: string;
  tenant_id: string;
  user_id: string;
  status: RunStatus;
  created_at: string;
  error: string | null;
  /** Role -> model this run used. Empty for runs from before routing. */
  models?: Record<string, string>;
  /** Set when the run ends or pauses; absent while running and for older runs. */
  usage?: RunUsage | null;
  events_url?: string;
  cancel_url?: string;
}

export interface ChatMessage {
  id: string | null;
  type: "human" | "ai" | "tool" | "system" | string;
  content: string;
  name?: string;
  tool_calls?: { id: string; name: string; args: Record<string, unknown> }[];
  /** The model that wrote an AI message, as the API reported it. */
  model?: string;
}

export interface InterruptPayload {
  kind: string;
  topic?: string;
  plan?: string[];
  /** Why the planner asked each question, index-aligned with `plan`. */
  rationale?: string[];
  web_search?: boolean;
  /** The corpus each question will also search; answer `documents: false` to skip it. */
  corpus?: { corpus_id: string; name: string } | null;
  /** Role -> model for the run this plan belongs to. */
  models?: Record<string, string>;
  prompt?: string;
  [key: string]: unknown;
}

export interface Interrupt {
  id: string | null;
  value: InterruptPayload;
}

export interface Snapshot {
  checkpoint_id: string | null;
  parent_checkpoint_id: string | null;
  created_at: string | null;
  next: string[];
  values: Record<string, unknown> & { messages?: ChatMessage[] };
  metadata: Record<string, unknown>;
  interrupts: Interrupt[];
  tasks: { id: string; name: string; error: string | null }[];
}

/** Mirrors `RoleUsage` in src/waypoint/schemas.py. */
export interface RoleUsage {
  model: string;
  calls?: number;
  /** All input, cached parts included. */
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  searches?: number;
  /** Embedding (estimated) or rerank tokens. */
  tokens?: number;
  /** null: the model is not in the price table. */
  cost_usd: number | null;
}

/** Mirrors `RunUsage` in src/waypoint/schemas.py. */
export interface RunUsage {
  /** null when any call was by a model the price table does not list. */
  cost_usd: number | null;
  input_tokens: number;
  output_tokens: number;
  searches: number;
  roles: Record<string, RoleUsage>;
}

// --- SSE frames -----------------------------------------------------------

export type RunEvent =
  | {
      type: "run.started";
      data: { run_id: string; thread_id: string; agent: string; models?: Record<string, string> };
    }
  | { type: "token"; data: { text: string; node: string | null } }
  | { type: "node"; data: { node: string; update: unknown } }
  | { type: "custom"; data: { payload: unknown } }
  | { type: "heartbeat"; data: { ts: string } }
  | {
      type: "run.interrupted";
      data: { interrupts: Interrupt[]; next: string[]; usage?: RunUsage | null };
    }
  | { type: "run.completed"; data: { result: RunResult; usage?: RunUsage | null } }
  | { type: "run.failed"; data: { error: string; type: string; usage?: RunUsage | null } }
  | { type: "run.cancelled"; data: { run_id: string; usage?: RunUsage | null } };

/** Mirrors `Source` / `Finding` in src/waypoint/agents/state.py. */
export interface Source {
  /** A web URL, or `doc://<document_id>#<chunk>` for a passage from the tenant's documents. */
  url: string;
  /** Page title, or the passage's heading path. */
  title: string;
  /** Absent means web. */
  kind?: "web" | "document";
  quotes?: string[];
}

/** Mirrors `CorpusOut` in src/waypoint/schemas.py. */
export interface Corpus {
  corpus_id: string;
  tenant_id: string;
  name: string;
  visibility: "private" | "public";
  embed_model: string;
  created_at: string;
  documents: number;
  chunks: number;
}

export interface Finding {
  question: string;
  answer: string;
  sources: Source[];
  /** False: the searcher cited nothing and `sources` are its search results. Absent means cited. */
  cited?: boolean;
  /** Set by the critic; null until reviewed. */
  confidence: number | null;
  /** The critic's note on what is weak or missing; empty when nothing is. */
  issue: string;
  round: number;
  /** The searcher model that wrote `answer`. */
  model?: string;
}

export interface RunResult {
  message?: ChatMessage;
  report?: string;
  plan?: string[];
  findings?: Finding[];
  topic?: string;
  step_count?: number;
  metadata?: Record<string, unknown>;
}

export const TERMINAL_EVENTS = new Set([
  "run.completed",
  "run.failed",
  "run.cancelled",
  "run.interrupted",
]);

export interface Problem {
  type: string;
  title: string;
  status: number;
  detail: string;
  request_id?: string;
  errors?: unknown[];
  [key: string]: unknown;
}
