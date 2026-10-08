import type { AgentSpec, Thread } from "../api/types";

interface Props {
  agents: AgentSpec[];
  threads: Thread[];
  selected: string | null;
  agent: string;
  onAgent: (name: string) => void;
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
