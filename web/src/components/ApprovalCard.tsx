import { useState } from "react";

import type { Interrupt } from "../api/types";
import { ModelLine } from "./ResearchProgress";

interface Props {
  interrupt: Interrupt;
  busy: boolean;
  onDecide: (decision: Record<string, unknown>) => void;
}

/**
 * The UI half of `interrupt()`. The graph is checkpointed and idle in Postgres
 * while this card is on screen -- closing the tab loses nothing, and the plan
 * can be approved tomorrow from another device.
 */
export function ApprovalCard({ interrupt, busy, onDecide }: Props) {
  const initial = interrupt.value.plan ?? [];
  const rationale = interrupt.value.rationale ?? [];
  const [plan, setPlan] = useState<string[]>(initial);
  const corpus = interrupt.value.corpus ?? null;
  const [documents, setDocuments] = useState(true);
  // Rationale belongs to the planner's question, so only show it while the
  // question is still the planner's text.
  const why = (question: string) => rationale[initial.indexOf(question)] ?? "";

  const edit = (index: number, text: string) =>
    setPlan((prev) => prev.map((q, i) => (i === index ? text : q)));

  return (
    <section className="card approval">
      <header>
        <span className="badge badge--warn">awaiting approval</span>
        <h3>{interrupt.value.prompt ?? "Approve to continue"}</h3>
      </header>
      {interrupt.value.topic && <p className="muted">Topic: {interrupt.value.topic}</p>}
      {interrupt.value.models && <ModelLine models={interrupt.value.models} />}
      {interrupt.value.web_search === false && (
        <p className="muted">Web search is off: answers will come from the model alone, without sources.</p>
      )}
      {corpus && (
        <label className="row small">
          <input
            type="checkbox"
            checked={documents}
            disabled={busy}
            onChange={(event) => setDocuments(event.target.checked)}
          />
          Also search your documents: <strong>{corpus.name}</strong>
        </label>
      )}

      {plan.length > 0 && (
        <ol className="plan">
          {plan.map((question, index) => (
            <li key={index}>
              <input
                value={question}
                disabled={busy}
                onChange={(event) => edit(index, event.target.value)}
                aria-label={`Question ${index + 1}`}
              />
              <button
                type="button"
                className="link"
                disabled={busy}
                onClick={() => setPlan((prev) => prev.filter((_, i) => i !== index))}
              >
                remove
              </button>
              {why(question) && <small className="muted plan__why">{why(question)}</small>}
            </li>
          ))}
        </ol>
      )}

      <div className="row">
        <button
          type="button"
          className="primary"
          disabled={busy || plan.length === 0}
          onClick={() => onDecide({ approved: true, plan, ...(corpus ? { documents } : {}) })}
        >
          Approve and run
        </button>
        <button type="button" disabled={busy} onClick={() => onDecide({ approved: false })}>
          Decline
        </button>
      </div>
    </section>
  );
}
