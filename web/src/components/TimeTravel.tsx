import type { Snapshot } from "../api/types";

interface Props {
  history: Snapshot[];
  busy: boolean;
  onFork: (checkpointId: string) => void;
}

/**
 * Every super-step of the graph left a checkpoint. Forking from one rewinds the
 * thread to that moment and continues down a new branch -- the graph equivalent
 * of `git checkout -b` on a conversation.
 */
export function TimeTravel({ history, busy, onFork }: Props) {
  if (history.length === 0) return null;

  return (
    <section className="card">
      <h3>Checkpoints</h3>
      <ul className="timeline">
        {history.map((snapshot) => {
          const step = snapshot.metadata["step"];
          const source = String(snapshot.metadata["source"] ?? "");
          return (
            <li key={snapshot.checkpoint_id ?? Math.random()}>
              <div>
                <code>{snapshot.checkpoint_id?.slice(0, 8)}</code>
                <span className="muted">
                  {" "}
                  step {String(step ?? "?")} · {source}
                  {snapshot.next.length > 0 && ` · next: ${snapshot.next.join(", ")}`}
                </span>
              </div>
              <button
                type="button"
                className="link"
                disabled={busy || !snapshot.checkpoint_id}
                onClick={() => snapshot.checkpoint_id && onFork(snapshot.checkpoint_id)}
              >
                resume from here
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
