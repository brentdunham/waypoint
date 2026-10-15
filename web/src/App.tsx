import { useCallback, useEffect, useMemo, useState } from "react";

import { api, auth } from "./api/client";
import type { ChatMessage, Corpus, Finding } from "./api/types";
import { isToolApproval } from "./approvals";
import { ApprovalCard } from "./components/ApprovalCard";
import { ConnectionBar } from "./components/ConnectionBar";
import { ResearchProgress } from "./components/ResearchProgress";
import { RunTimeline } from "./components/RunTimeline";
import { Sidebar } from "./components/Sidebar";
import { TimeTravel } from "./components/TimeTravel";
import { ToolApprovalCard } from "./components/ToolApprovalCard";
import { Transcript } from "./components/Transcript";
import { Welcome } from "./components/Welcome";
import { useRun } from "./hooks/useRun";
import { useAgents, useSnapshot, useThreads } from "./hooks/useThreads";
import { formatUsage, usageBreakdown } from "./usage";

export default function App() {
  const { agents } = useAgents();
  const { threads, refresh: refreshThreads } = useThreads();
  const [agent, setAgent] = useState("chat");
  const [threadId, setThreadId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [corpora, setCorpora] = useState<Corpus[]>([]);
  const [corpusId, setCorpusId] = useState("");
  // Workspace selects several corpora at once (docs/design/tools.md, T10).
  const [corpusIds, setCorpusIds] = useState<string[]>([]);

  // Corpora are optional: a server without RAG answers 503 and the picker hides.
  const refreshCorpora = useCallback(
    () => api.corpora().then(setCorpora, () => setCorpora([])),
    [],
  );
  useEffect(() => {
    void refreshCorpora();
  }, [refreshCorpora]);

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
  // A workspace thread's documents are in its state: show them in the picker
  // when the thread is opened or a run ends.
  const threadCorpora = snapshot?.values["corpus_ids"] as string[] | undefined;
  const threadCorporaKey = threadCorpora?.join(",");
  useEffect(() => {
    if (activeAgent === "workspace" && threadId && threadCorporaKey !== undefined) {
      setCorpusIds(threadCorporaKey ? threadCorporaKey.split(",") : []);
    }
  }, [activeAgent, threadId, threadCorporaKey]);

  // Research findings get their own card; a workspace reply lists its sources inline.
  const findings: Finding[] = run.busy || activeAgent !== "research"
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
      // Research: the corpus goes with the run's first input, and the approval
      // card can still switch it off. Workspace: every message carries the
      // picker's selection, and the thread keeps it (an empty list clears it).
      if (activeAgent === "workspace") {
        await run.launch(id, { message, input: { corpus_ids: corpusIds } });
        return;
      }
      const withDocuments = agent === "research" && corpusId && !threadId;
      await run.launch(id, withDocuments ? { message, input: { corpus_id: corpusId } } : { message });
    });

  /** One click from the welcome panel: a new thread for that agent, then run.
   *  An example over a corpus names it; it runs on the web alone if that corpus
   *  is not on this server. */
  const tryExample = (name: string, text: string, corpusName?: string) =>
    void guard(async () => {
      setAgent(name);
      run.reset();
      const corpus = corpusName ? corpora.find((c) => c.name === corpusName) : undefined;
      setCorpusId(corpus?.corpus_id ?? "");
      setCorpusIds(corpus ? [corpus.corpus_id] : []);
      const id = (await api.createThread(name, text.slice(0, 60))).thread_id;
      setThreadId(id);
      await refreshThreads();
      await run.launch(
        id,
        !corpus
          ? { message: text }
          : name === "workspace"
            ? { message: text, input: { corpus_ids: [corpus.corpus_id] } }
            : { message: text, input: { corpus_id: corpus.corpus_id } },
      );
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
      {run.recoveryNotice && <p role="status">{run.recoveryNotice}</p>}
      <div className="layout">
        <Sidebar
          agents={agents}
          threads={threads}
          selected={threadId}
          agent={agent}
          corpora={corpora}
          corpusId={corpusId}
          onCorpus={setCorpusId}
          corpusIds={corpusIds}
          onCorpusIds={setCorpusIds}
          onCorporaChanged={refreshCorpora}
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

          {messages.length === 0 && !run.busy && !pending && !run.streamedText && (
            <Welcome
              hasKey={auth.get() !== ""}
              corpora={corpora.map((c) => c.name)}
              onTry={tryExample}
            />
          )}

          <Transcript
            messages={messages}
            streaming={run.streamedText}
            streamingModel={run.progress.models["writer"] ?? run.progress.models["assistant"]}
            activeNodes={run.nodes}
          />

          {/* What the last run cost. A research run pays for its plan and its
              briefing as two runs, so the plan's line says so. */}
          {run.usage && !run.busy && (
            <p className="muted small run-usage" title={usageBreakdown(run.usage)}>
              {run.status === "interrupted" && (activeAgent === "research" ? "Plan: " : "So far: ")}
              {formatUsage(run.usage, run.durationMs)}
            </p>
          )}
          {run.runId && run.usage && !run.busy && <RunTimeline runId={run.runId} />}

          {activeAgent === "research" && <ResearchProgress progress={run.progress} />}

          {pending && isToolApproval(pending.value) && (
            <ToolApprovalCard
              key={pending.id ?? JSON.stringify(pending.value.calls)}
              interrupt={pending}
              busy={run.busy}
              onDecide={decide}
            />
          )}
          {pending && !isToolApproval(pending.value) && (
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
                      {f.model && ` · ${f.model}`}
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
                            {s.kind === "document" ? (
                              // A passage from the tenant's corpus: no URL to open,
                              // so show its heading path and what was quoted.
                              <>
                                <span className="badge badge--doc">doc</span> {s.title}
                                {s.quotes?.[0] && <q className="muted small"> {s.quotes[0]}</q>}
                              </>
                            ) : (
                              <a href={s.url} target="_blank" rel="noreferrer noopener">
                                {s.title}
                              </a>
                            )}
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
                activeAgent === "research"
                  ? "Topic to research…"
                  : activeAgent === "workspace" && corpusIds.length > 0
                    ? "Ask about your documents…"
                    : "Ask something…"
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
