import { useState } from "react";

import type { Interrupt } from "../api/types";
import { formatArgs, pendingCalls, toolDecision } from "../approvals";

interface Props {
  interrupt: Interrupt;
  busy: boolean;
  onDecide: (decision: Record<string, unknown>) => void;
}

/**
 * Workspace paused before tool calls that could change something
 * (docs/design/tools.md, T4). Nothing has run yet; the run waits in its
 * checkpoint, so this card comes back after a reload. Each call is allowed
 * or not on its own; a declined call is reported to the model, which tells
 * the person it was not done.
 */
export function ToolApprovalCard({ interrupt, busy, onDecide }: Props) {
  const calls = pendingCalls(interrupt.value);
  const [allowed, setAllowed] = useState<Record<string, boolean>>(
    Object.fromEntries(calls.map((c) => [c.id, true])),
  );
  const chosen = calls.filter((c) => allowed[c.id]).length;

  return (
    <section className="card approval">
      <header>
        <span className="badge badge--warn">awaiting approval</span>
        <h3>{interrupt.value.prompt ?? "Allow these tool calls?"}</h3>
      </header>
      <ul className="tool-calls">
        {calls.map((call) => (
          <li key={call.id}>
            <label className="row">
              <input
                type="checkbox"
                checked={allowed[call.id] ?? false}
                disabled={busy}
                onChange={(event) => setAllowed((prev) => ({ ...prev, [call.id]: event.target.checked }))}
              />
              <strong>{call.remote_tool}</strong>
              <span className="muted small">
                on {call.server}
                {call.trusted ? "" : " (untrusted server)"}
              </span>
            </label>
            {call.description && <p className="muted small">{call.description}</p>}
            <pre className="tool-calls__args">{formatArgs(call.args)}</pre>
          </li>
        ))}
      </ul>
      <div className="row">
        <button type="button" className="primary" disabled={busy} onClick={() => onDecide(toolDecision(calls, allowed))}>
          {chosen === calls.length ? "Allow" : chosen === 0 ? "Decline all" : `Allow ${chosen} of ${calls.length}`}
        </button>
        <button type="button" disabled={busy} onClick={() => onDecide(toolDecision(calls, {}))}>
          Decline all
        </button>
      </div>
    </section>
  );
}
