import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import type { AgentSpec, Snapshot, Thread } from "../api/types";

export function useAgents() {
  const [agents, setAgents] = useState<AgentSpec[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.agents().then(setAgents).catch((e: unknown) => setError(String(e)));
  }, []);
  return { agents, error };
}

export function useThreads() {
  const [threads, setThreads] = useState<Thread[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setThreads(await api.listThreads());
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { threads, refresh, error };
}

export function useSnapshot(threadId: string | null) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [history, setHistory] = useState<Snapshot[]>([]);

  const refresh = useCallback(async () => {
    if (!threadId) {
      setSnapshot(null);
      setHistory([]);
      return;
    }
    const [head, past] = await Promise.all([
      api.state(threadId),
      api.history(threadId, 30),
    ]);
    setSnapshot(head);
    setHistory(past);
  }, [threadId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { snapshot, history, refresh };
}
