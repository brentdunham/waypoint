import type { AgentSpec, Corpus, Thread } from "../api/types";
import { CorpusManager } from "./CorpusManager";

/** Agents that can search a corpus. Research takes one per run (its first
 *  message); a workspace thread keeps the one it was last sent. */
export const DOCUMENT_AGENTS = ["research", "workspace"];

/** Corpora one workspace thread may select; the server's WORKSPACE__MAX_CORPORA
 *  default. The server enforces its own setting; this keeps the UI honest. */
export const MAX_CORPORA = 5;

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
  /** Workspace: the selected corpora, several at once. */
  corpusIds: string[];
  onCorpusIds: (corpusIds: string[]) => void;
  /** Reload the corpora after one is created or its documents finish indexing. */
  onCorporaChanged: () => Promise<void>;
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
  corpusIds,
  onCorpusIds,
  onCorporaChanged,
  onSelect,
  onNew,
  onDelete,
}: Props) {
  const spec = agents.find((a) => a.name === agent);
  // A server with retrieval on has at least the public docs corpus.
  const ragOn = corpora.length > 0;
  // Uploads start ingest runs on threads of their own; they are not conversations.
  const visible = threads.filter((t) => agents.length === 0 || agents.some((a) => a.name === t.agent));
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
      {agent === "workspace" && corpora.length > 0 && (
        <CorpusChecklist corpora={corpora} selected={corpusIds} onChange={onCorpusIds} />
      )}
      {agent === "workspace" && ragOn && (
        <CorpusManager
          corpora={corpora.filter((c) => c.visibility !== "public")}
          onChanged={onCorporaChanged}
          onCreated={(id) => {
            if (!corpusIds.includes(id) && corpusIds.length < MAX_CORPORA) {
              onCorpusIds([...corpusIds, id]);
            }
          }}
        />
      )}
      {agent === "research" && corpora.length > 0 && (
        <label className="field">
          <span>Documents</span>
          <select value={corpusId} onChange={(event) => onCorpus(event.target.value)}>
            <option value="">Web only</option>
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
        {visible.map((thread) => (
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
        {visible.length === 0 && <li className="muted small">No threads yet.</li>}
      </ul>
    </aside>
  );
}

/** Workspace's documents: global (public) corpora, then the tenant's own, each
 *  a checkbox. One search covers every checked corpus. */
function CorpusChecklist({
  corpora,
  selected,
  onChange,
}: {
  corpora: Corpus[];
  selected: string[];
  onChange: (corpusIds: string[]) => void;
}) {
  const groups: [string, Corpus[]][] = [
    ["Global", corpora.filter((c) => c.visibility === "public")],
    ["Yours", corpora.filter((c) => c.visibility !== "public")],
  ];
  const full = selected.length >= MAX_CORPORA;
  const toggle = (id: string, on: boolean) =>
    onChange(on ? [...selected, id] : selected.filter((s) => s !== id));
  return (
    <fieldset className="field corpora">
      <legend>Documents</legend>
      {groups.map(
        ([label, list]) =>
          list.length > 0 && (
            <div key={label} className="corpora__group">
              <span className="muted small">{label}</span>
              {list.map((c) => {
                const on = selected.includes(c.corpus_id);
                return (
                  <label key={c.corpus_id} className="corpora__item">
                    <input
                      type="checkbox"
                      checked={on}
                      disabled={!on && full}
                      onChange={(event) => toggle(c.corpus_id, event.target.checked)}
                    />{" "}
                    {c.name} <span className="muted small">({c.documents} docs)</span>
                  </label>
                );
              })}
            </div>
          ),
      )}
      <span className="muted small">
        {selected.length === 0
          ? "None selected: answers from the model alone."
          : `${selected.length} selected${full ? ` (at most ${MAX_CORPORA})` : ""}.`}
      </span>
    </fieldset>
  );
}
