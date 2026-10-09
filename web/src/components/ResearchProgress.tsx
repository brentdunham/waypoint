import { isActive, type Progress } from "../research/progress";

const LABEL: Record<Progress["phase"], string> = {
  idle: "",
  planning: "planning questions",
  "awaiting approval": "awaiting approval",
  searching: "searching",
  reviewing: "critic reviewing findings",
  writing: "writing the briefing",
  done: "done",
  declined: "plan declined",
  failed: "failed",
  cancelled: "cancelled",
};

/** Live view of a research run: one row per worker, with its round and status. */
export function ResearchProgress({ progress }: { progress: Progress }) {
  const { phase, round, workers } = progress;
  if (phase === "idle" || (workers.length === 0 && !isActive(phase))) return null;
  const done = workers.filter((w) => w.status === "done").length;

  return (
    <section className="card progress" aria-live="polite">
      <header className="progress__head">
        <h3>Research</h3>
        <span className={`badge ${isActive(phase) ? "badge--live" : phase === "done" ? "badge--ok" : "badge--warn"}`}>
          {LABEL[phase]}
          {phase === "searching" && round > 1 && ` · round ${round}`}
        </span>
        {workers.length > 0 && (
          <span className="muted small">
            {done}/{workers.length} workers done
          </span>
        )}
      </header>
      {workers.length > 0 && (
        <ul className="workers">
          {workers.map((w) => (
            <li key={`${w.round}-${w.question}`} className={`worker worker--${w.status}`}>
              <span className="worker__dot" aria-label={w.status} />
              <div>
                <div>{w.question}</div>
                <div className="muted small">
                  {w.round > 1 && <span className="chip">follow-up</span>}
                  {w.status === "searching" ? (
                    "searching the web…"
                  ) : (
                    <>
                      {w.sources} source{w.sources === 1 ? "" : "s"}
                      {!w.cited && " · not cited, search results only"}
                      {w.confidence !== null && ` · confidence ${w.confidence.toFixed(1)}`}
                    </>
                  )}
                </div>
                {w.issue && <div className="muted small">Critic: {w.issue}</div>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
