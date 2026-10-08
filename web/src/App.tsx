import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api/client";
import type { ChatMessage } from "./api/types";
import { ApprovalCard } from "./components/ApprovalCard";
import { ConnectionBar } from "./components/ConnectionBar";
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
  const pending = snapshot?.interrupts?.[0] ?? run.interrupts[0] ?? null;

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
      const thread = await api.createThread(agent);
      setThreadId(thread.thread_id);
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
          onAgent={setAgent}
          onSelect={(id) => {
            setThreadId(id);
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

          {pending && <ApprovalCard interrupt={pending} busy={run.busy} onDecide={decide} />}

          {/* The briefing itself is the last assistant message in the transcript;
              this card adds what the transcript does not show. */}
          {run.result?.findings && run.result.findings.length > 0 && (
            <section className="card">
              <h3>Findings</h3>
              <ul className="findings">
                {run.result.findings.map((f) => (
                  <li key={f.question}>
                    <strong>{f.question}</strong>
                    <span className="muted">
                      {" · "}
                      {f.confidence === null ? "unreviewed" : `confidence ${f.confidence.toFixed(1)}`}
                      {f.round > 1 && " · follow-up"}
                    </span>
                    <p>{f.answer}</p>
                    {f.issue && <p className="muted">Reviewer: {f.issue}</p>}
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
                agent === "research" ? "Topic to research…" : "Ask something…"
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
