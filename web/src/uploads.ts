import type { CorpusDocument, UploadUsage } from "./api/types";

const count = new Intl.NumberFormat("en-US");

function mb(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** "Today: 2/10 files · 0.3/5.0 MB · 120/2,000 chunks. Resets at midnight UTC."
 *  Admin keys are not limited, so they get no line. */
export function formatUploadUsage(usage: UploadUsage): string | null {
  if (!usage.limited) return null;
  return (
    `Today: ${usage.files}/${usage.files_per_day} files · ` +
    `${mb(usage.bytes)}/${mb(usage.bytes_per_day)} · ` +
    `${count.format(usage.chunks)}/${count.format(usage.chunks_per_day)} chunks. ` +
    "Resets at midnight UTC."
  );
}

/** Whether a new corpus would pass the cap. */
export function canCreateCorpus(usage: UploadUsage | null): boolean {
  return !usage || !usage.limited || usage.corpora < usage.max_corpora;
}

/** Still being indexed: poll until none are. */
export function anyPending(documents: CorpusDocument[]): boolean {
  return documents.some((d) => d.status === "pending");
}

/** What the list shows beside a document. */
export function documentStatus(doc: CorpusDocument): string {
  if (doc.status === "pending") return "indexing…";
  if (doc.status === "failed") return "failed";
  return `${count.format(doc.chunks)} ${doc.chunks === 1 ? "chunk" : "chunks"}`;
}
