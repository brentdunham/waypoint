import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api/client";
import type { ChatMessage, Finding } from "./api/types";
import { ApprovalCard } from "./components/ApprovalCard";
import { ConnectionBar } from "./components/ConnectionBar";
import { ResearchProgress } from "./components/ResearchProgress";
import { Sidebar } from "./components/Sidebar";
import { TimeTravel } from "./components/TimeTravel";
import { Transcript } from "./components/Transcript";
import { useRun } from "./hooks/useRun";
import { useAgents, useSnapshot, useThreads } from "./hooks/useThreads";

export default function App() {
  const { agents } = useAgents();
  const { threads, refresh: refreshThreads } = useThreads();
  const [agent, setAgent] = useState("chat");
  const [threadId, setThreadId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const { snapshot, history, refresh: refreshState } = useSnapshot(threadId);
  const run = useRun(async () => {
    await Promise.all([refreshState(), refreshThreads()]);
  });

  useEffect(() => {
    if (agents.length > 0 && !agents.some((a) => a.name === agent)) {
      setAgent(agents[0]?.name ?? "chat");
    }
  }, [agents, agent]);

  const messages = useMemo<ChatMessage[]>(
    () => (snapshot?.values.messages ?? []) as ChatMessage[],
    [snapshot],
  );
  // While a run is going, the snapshot is the one fetched before it started:
  // after "Approve and run" it still holds the interrupt that was just
  // answered. Only a settled thread can be waiting for a decision.
  const pending = run.busy ? null : (run.interrupts[0] ?? snapshot?.interrupts?.[0] ?? null);
  const thread = threads.find((t) => t.thread_id === threadId);
  // The open thread decides which agent a message goes to, not the picker.
  const activeAgent = thread?.agent ?? agent;
  const findings: Finding[] = run.busy
    ? []
    : (run.result?.findings ?? (snapshot?.values["findings"] as Finding[] | undefined) ?? []);

  const guard = useCallback(async (fn: () => Promise<void>) => {
    try {
      setNotice(null);
      await fn();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error));
    }
  }, []);

  const newThread = () =>
    void guard(async () => {
      const created = await api.createThread(agent);
      setThreadId(created.thread_id);
      run.reset();
      await refreshThreads();
    });

  const send = () =>
    void guard(async () => {
      if (!draft.trim()) return;
      let id = threadId;
      if (!id) {
        id = (await api.createThread(agent, draft.slice(0, 60))).thread_id;
        setThreadId(id);
      }
      const message = draft;
      setDraft("");
      await run.launch(id, { message });
    });

  const decide = (decision: Record<string, unknown>) =>
    void guard(async () => {
      if (!threadId) return;
      await run.launch(threadId, { resume: decision });
    });

  const fork = (checkpointId: string) =>
    void guard(async () => {
      if (!threadId) return;
      await run.launch(threadId, { input: {}, checkpoint_id: checkpointId });
    });

  return (
    <div className="shell">
      <ConnectionBar />
      <div className="layout">
        <Sidebar
          agents={agents}
          threads={threads}
          selected={threadId}
          agent={agent}
          onAgent={(name) => {
            setAgent(name);
            // A thread belongs to one agent; picking another starts fresh.
            if (thread && thread.agent !== name) setThreadId(null);
            run.reset();
          }}
          onSelect={(id) => {
            setThreadId(id);
            const picked = threads.find((t) => t.thread_id === id);
            if (picked) setAgent(picked.agent);
            run.reset();
          }}
          onNew={newThread}
          onDelete={(id) =>
            void guard(async () => {
              await api.deleteThread(id);
              if (id === threadId) setThreadId(null);
              await refreshThreads();
            })
          }
        />

        <main className="main">
          {notice && <p className="banner banner--error">{notice}</p>}
          {run.error && <p className="banner banner--error">{run.error}</p>}

          <Transcript messages={messages} streaming={run.streamedText} activeNodes={run.nodes} />

          {activeAgent === "research" && <ResearchProgress progress={run.progress} />}

          {pending && (
            <ApprovalCard
              key={pending.id ?? JSON.stringify(pending.value.plan)}
              interrupt={pending}
              busy={run.busy}
              onDecide={decide}
            />
          )}

          {/* The briefing itself is the last assistant message in the transcript;
              this card adds what the transcript does not show. */}
          {findings.length > 0 && (
            <section className="card">
              <h3>Findings</h3>
              <ul className="findings">
                {findings.map((f) => (
                  <li key={f.question}>
                    <strong>{f.question}</strong>
                    <span className="muted">
                      {" · "}
                      {f.confidence === null ? "unreviewed" : `confidence ${f.confidence.toFixed(1)}`}
                      {f.round > 1 && " · follow-up"}
                    </span>
                    <p>{f.answer}</p>
                    {f.issue && <p className="muted">Reviewer: {f.issue}</p>}
                    {f.cited === false && f.sources.length > 0 && (
                      <p className="muted small">
                        Not cited: these are the pages its searches returned, not sources for a specific claim.
                      </p>
                    )}
                    {f.sources.length > 0 ? (
                      <ul className="sources">
                        {f.sources.map((s) => (
                          <li key={s.url}>
                            <a href={s.url} target="_blank" rel="noreferrer noopener">
                              {s.title}
                            </a>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="muted">No sources.</p>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              send();
            }}
          >
            <textarea
              value={draft}
              placeholder={
                activeAgent === "research" ? "Topic to research…" : "Ask something…"
              }
              rows={2}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  send();
                }
              }}
            />
            {run.busy ? (
              <button type="button" className="danger" onClick={() => void run.cancel()}>
                Cancel
              </button>
            ) : (
              <button type="submit" className="primary" disabled={!draft.trim()}>
                Send
              </button>
            )}
          </form>

          <TimeTravel history={history} busy={run.busy} onFork={fork} />
        </main>
      </div>
    </div>
  );
}
