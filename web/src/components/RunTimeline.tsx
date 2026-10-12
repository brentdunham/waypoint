import { useEffect, useState } from "react";

import { api, ApiError } from "../api/client";
import type { RunTrace, TraceStep } from "../api/types";
import { describe, formatDuration, rows, summarize, summaryLine } from "../timeline";
import { formatCost } from "../usage";

const POLL_MS = 2_000;
const POLL_TRIES = 15;
/** Spans reach the trace store in batches, a few seconds after the run ends. */

interface Props {
  runId: string;
}

/**
 * "How this run went": the run's steps on one time axis, each with what ran
 * inside it (model calls, document searches, tools), from the run's trace.
 * Renders nothing when the server has run timelines off (404).
 */
export function RunTimeline({ runId }: Props) {
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [gone, setGone] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setTrace(null);
    setGone(false);
    setOpen(new Set());
    const load = async (attempt: number) => {
      try {
        const found = await api.runTrace(runId);
        if (cancelled) return;
        if (found.status === "ready") setTrace(found);
        else if (attempt < POLL_TRIES) timer = setTimeout(() => void load(attempt + 1), POLL_MS);
        else setGone(true);
      } catch (error) {
        // 404: timelines are off here, or the run was not traced. Either way, nothing to show.
        if (!cancelled) setGone(true);
        if (!(error instanceof ApiError)) console.warn("run timeline", error);
      }
    };
    void load(1);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [runId]);

  if (gone) return null;
  if (!trace) return <p className="muted small timeline-wait">Loading this run's timeline…</p>;

  const total = Math.max(trace.duration_ms ?? 0, 1);
  const lines = rows(trace);
  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <details className="card timeline">
      <summary>
        <strong>How this run went</strong>
        <span className="muted"> · {summaryLine(summarize(trace))}</span>
      </summary>

      <ul className="timeline__legend small muted" aria-label="Legend">
        <li><i className="swatch swatch--step" /> step</li>
        <li><i className="swatch swatch--model" /> model call</li>
        <li><i className="swatch swatch--search" /> document search</li>
        <li><i className="swatch swatch--tool" /> tool</li>
      </ul>

      <div className="timeline__grid" role="table" aria-label="Run timeline">
        <div className="timeline__axis small muted" role="row" aria-hidden="true">
          <span />
          <span className="timeline__ticks">
            {[0, 0.25, 0.5, 0.75, 1].map((f) => (
              <span key={f} style={{ left: `${f * 100}%` }}>{f === 0 ? "0s" : formatDuration(f * total)}</span>
            ))}
          </span>
          <span />
        </div>

        {lines.map((row) => {
          const expanded = open.has(row.step.id);
          return (
            <div key={row.step.id} className="timeline__group" role="rowgroup">
              <button
                type="button"
                className={`timeline__row${row.step.error ? " timeline__row--error" : ""}`}
                role="row"
                aria-expanded={row.children.length ? expanded : undefined}
                onClick={() => row.children.length && toggle(row.step.id)}
                disabled={!row.children.length}
              >
                <span className="timeline__label" role="rowheader">
                  {row.children.length > 0 && <span className="timeline__caret">{expanded ? "▾" : "▸"}</span>}
                  {row.label}
                </span>
                <span className="timeline__track" role="cell">
                  <Bar step={row.step} total={total} kind="step" title={`${row.label} · ${formatDuration(row.step.duration_ms)}`} />
                  {row.children.map((c) => (
                    <Bar key={c.id} step={c} total={total} kind={kindClass(c)} title={describe(c)} />
                  ))}
                </span>
                <span className="timeline__meta small muted" role="cell">
                  {formatDuration(row.step.duration_ms)}
                  {row.costUsd > 0 && ` · ${formatCost(row.costUsd)}`}
                </span>
              </button>
              {expanded && (
                <ul className="timeline__children small">
                  {row.children.map((c) => (
                    <li key={c.id}>
                      <i className={`swatch swatch--${kindClass(c)}`} /> {describe(c)}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>
      <p className="small muted timeline__note">
        Times are wall clock from the start of the run; research workers run side by side.
        Click a step to see its model calls and searches.
      </p>
    </details>
  );
}

function kindClass(step: TraceStep): "model" | "search" | "tool" {
  if (step.kind === "retrieval") return "search";
  if (step.kind === "tool") return "tool";
  return "model";
}

function Bar({ step, total, kind, title }: { step: TraceStep; total: number; kind: string; title: string }) {
  const left = (step.start_ms / total) * 100;
  const width = Math.max((step.duration_ms / total) * 100, 0.4);
  return (
    <span
      className={`timeline__bar timeline__bar--${kind}`}
      style={{ left: `${Math.min(left, 99.6)}%`, width: `${Math.min(width, 100 - left)}%` }}
      title={title}
    />
  );
}
