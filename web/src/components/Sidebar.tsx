import type { AgentSpec, Corpus, Thread } from "../api/types";

/** Agents that can search a corpus. Research takes one per run (its first
 *  message); a workspace thread keeps the one it was last sent. */
export const DOCUMENT_AGENTS = ["research", "workspace"];

interface Props {
  agents: AgentSpec[];
  threads: Thread[];
  selected: string | null;
  agent: string;
  onAgent: (name: string) => void;
  /** Corpora the research and workspace agents can search; the picker hides when there are none. */
  corpora: Corpus[];
  corpusId: string;
  onCorpus: (corpusId: string) => void;
  onSelect: (threadId: string) => void;
  onNew: () => void;
  onDelete: (threadId: string) => void;
}

export function Sidebar({
  agents,
  threads,
  selected,
  agent,
  onAgent,
  corpora,
  corpusId,
  onCorpus,
  onSelect,
  onNew,
  onDelete,
}: Props) {
  const spec = agents.find((a) => a.name === agent);
  return (
    <aside className="sidebar">
      <label className="field">
        <span>Agent</span>
        <select value={agent} onChange={(event) => onAgent(event.target.value)}>
          {agents.map((a) => (
            <option key={a.name} value={a.name}>
              {a.title}
            </option>
          ))}
        </select>
      </label>
      {spec && <p className="muted small">{spec.description}</p>}
      {DOCUMENT_AGENTS.includes(agent) && corpora.length > 0 && (
        <label className="field">
          <span>Documents</span>
          <select value={corpusId} onChange={(event) => onCorpus(event.target.value)}>
            <option value="">{agent === "research" ? "Web only" : "No documents"}</option>
            {corpora.map((c) => (
              <option key={c.corpus_id} value={c.corpus_id}>
                {c.name} ({c.documents} docs{c.visibility === "public" ? ", public" : ""})
              </option>
            ))}
          </select>
        </label>
      )}

      <button type="button" className="primary block" onClick={onNew}>
        New thread
      </button>

      <ul className="threads">
        {threads.map((thread) => (
          <li key={thread.thread_id} className={thread.thread_id === selected ? "on" : ""}>
            <button type="button" className="link grow" onClick={() => onSelect(thread.thread_id)}>
              <strong>{thread.title || "untitled"}</strong>
              <span className="muted small"> {thread.agent}</span>
            </button>
            <button
              type="button"
              className="link danger"
              aria-label="Delete thread"
              onClick={() => onDelete(thread.thread_id)}
            >
              ×
            </button>
          </li>
        ))}
        {threads.length === 0 && <li className="muted small">No threads yet.</li>}
      </ul>
    </aside>
  );
}
