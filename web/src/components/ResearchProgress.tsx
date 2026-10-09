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

const ROLE_ORDER = ["planner", "searcher", "critic", "writer"];

/** Role -> model, in pipeline order, as "planner claude-x · searcher claude-y …". */
export function ModelLine({ models }: { models: Record<string, string> }) {
  const roles = Object.keys(models).sort(
    (a, b) => (ROLE_ORDER.indexOf(a) + 1 || 99) - (ROLE_ORDER.indexOf(b) + 1 || 99),
  );
  if (roles.length === 0) return null;
  return (
    <p className="muted small models">
      {roles.map((role) => (
        <span key={role} className="models__item">
          {role} <code>{models[role]}</code>
        </span>
      ))}
    </p>
  );
}

/** Live view of a research run: one row per worker, with its round and status. */
export function ResearchProgress({ progress }: { progress: Progress }) {
  const { phase, round, workers, models } = progress;
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
      <ModelLine models={models} />
      {workers.length > 0 && (
        <ul className="workers">
          {workers.map((w) => (
            <li key={`${w.round}-${w.question}`} className={`worker worker--${w.status}`}>
              <span className="worker__dot" aria-label={w.status} />
              <div>
                <div>{w.question}</div>
                <div className="muted small">
                  {w.round > 1 && <span className="chip">follow-up</span>}
                  {w.model && <code className="worker__model">{w.model}</code>}
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
