import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../api/client";
import type { Corpus, CorpusDocument, UploadUsage } from "../api/types";
import { anyPending, canCreateCorpus, documentStatus, formatUploadUsage } from "../uploads";

const POLL_MS = 2000;
const ACCEPT = ".md,.markdown,.txt,.html,.htm,.pdf";

interface Props {
  /** The caller's own corpora; global ones are not uploaded to here. */
  corpora: Corpus[];
  /** Reload the corpus list (document counts, a new corpus). */
  onChanged: () => Promise<void>;
  /** A corpus was created: select it for the thread. */
  onCreated: (corpusId: string) => void;
}

/** Workspace's own documents (docs/design/tools.md, phase 2b): create a corpus,
 *  add files to it, and watch them index. The server enforces the limits; the
 *  usage line says where the caller stands. */
export function CorpusManager({ corpora, onChanged, onCreated }: Props) {
  const [usage, setUsage] = useState<UploadUsage | null>(null);
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [documents, setDocuments] = useState<CorpusDocument[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const corpusId = corpora.some((c) => c.corpus_id === target)
    ? target
    : (corpora[0]?.corpus_id ?? "");

  const refreshUsage = useCallback(() => {
    api.uploadUsage().then(setUsage, () => setUsage(null));
  }, []);

  const refreshDocuments = useCallback(async () => {
    if (!corpusId) {
      setDocuments([]);
      return;
    }
    setDocuments(await api.documents(corpusId).catch(() => []));
  }, [corpusId]);

  useEffect(refreshUsage, [refreshUsage]);
  useEffect(() => {
    void refreshDocuments();
  }, [refreshDocuments]);

  // While anything is indexing, check again every two seconds; when the last
  // one finishes, refresh the counts the checklist and the usage line show.
  const pending = anyPending(documents);
  useEffect(() => {
    if (!pending) return;
    const timer = window.setTimeout(() => {
      void refreshDocuments();
    }, POLL_MS);
    return () => window.clearTimeout(timer);
  }, [pending, documents, refreshDocuments]);
  useEffect(() => {
    if (!pending) {
      refreshUsage();
      void onChanged();
    }
    // Only when pending changes, not on every new onChanged.
  }, [pending]);

  const attempt = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 403
          ? "This key cannot add documents (it lacks corpora:write)."
          : caught instanceof Error
            ? caught.message
            : String(caught),
      );
    } finally {
      setBusy(false);
      refreshUsage();
    }
  };

  const create = () =>
    attempt(async () => {
      const corpus = await api.createCorpus(name.trim());
      setName("");
      setTarget(corpus.corpus_id);
      await onChanged();
      onCreated(corpus.corpus_id);
    });

  const upload = (files: File[]) =>
    attempt(async () => {
      if (!corpusId || files.length === 0) return;
      await api.upload(corpusId, files);
      await refreshDocuments();
    });

  const line = usage ? formatUploadUsage(usage) : null;
  return (
    <section className="field uploads">
      <span>Your documents</span>
      {corpora.length > 0 && (
        <>
          <select value={corpusId} onChange={(event) => setTarget(event.target.value)}>
            {corpora.map((c) => (
              <option key={c.corpus_id} value={c.corpus_id}>
                {c.name}
              </option>
            ))}
          </select>
          <label className="uploads__add">
            <input
              type="file"
              multiple
              accept={ACCEPT}
              disabled={busy}
              onChange={(event) => {
                const files = Array.from(event.target.files ?? []);
                event.target.value = "";
                void upload(files);
              }}
            />
          </label>
          {documents.length > 0 && (
            <ul className="uploads__docs">
              {documents.map((d) => (
                <li key={d.document_id} title={d.error ?? undefined}>
                  <span className="grow">{d.title}</span>{" "}
                  <span className={`muted small status-${d.status}`}>{documentStatus(d)}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {canCreateCorpus(usage) ? (
        <form
          className="uploads__new"
          onSubmit={(event) => {
            event.preventDefault();
            if (name.trim()) void create();
          }}
        >
          <input
            value={name}
            placeholder="New corpus name"
            maxLength={100}
            onChange={(event) => setName(event.target.value)}
          />
          <button type="submit" disabled={busy || !name.trim()}>
            Create
          </button>
        </form>
      ) : (
        <span className="muted small">
          {usage?.max_corpora} corpora is the limit; delete one to make another.
        </span>
      )}
      {line && <span className="muted small">{line}</span>}
      {error && <span className="uploads__error small">{error}</span>}
    </section>
  );
}
