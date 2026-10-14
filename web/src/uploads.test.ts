import { describe, expect, it } from "vitest";

import type { CorpusDocument, UploadUsage } from "./api/types";
import { anyPending, canCreateCorpus, documentStatus, formatUploadUsage } from "./uploads";

const USAGE: UploadUsage = {
  limited: true,
  corpora: 1,
  max_corpora: 3,
  files: 2,
  files_per_day: 10,
  bytes: 314_573,
  bytes_per_day: 5 * 1024 * 1024,
  chunks: 120,
  chunks_per_day: 2000,
  resets_at: "2026-10-15T00:00:00+00:00",
};

function doc(status: CorpusDocument["status"], chunks = 0): CorpusDocument {
  return {
    document_id: "d",
    corpus_id: "c",
    title: "a.md",
    media_type: "text/markdown",
    bytes: 10,
    status,
    error: status === "failed" ? "too many chunks" : null,
    created_at: "",
    chunks,
  };
}

describe("upload limits", () => {
  it("shows today's counts for a limited key and nothing for an admin", () => {
    expect(formatUploadUsage(USAGE)).toBe(
      "Today: 2/10 files · 0.3 MB/5.0 MB · 120/2,000 chunks. Resets at midnight UTC.",
    );
    expect(formatUploadUsage({ ...USAGE, limited: false })).toBeNull();
  });

  it("stops offering a new corpus at the cap", () => {
    expect(canCreateCorpus(USAGE)).toBe(true);
    expect(canCreateCorpus({ ...USAGE, corpora: 3 })).toBe(false);
    expect(canCreateCorpus({ ...USAGE, corpora: 3, limited: false })).toBe(true);
    expect(canCreateCorpus(null)).toBe(true);
  });
});

describe("indexing status", () => {
  it("polls while any document is pending", () => {
    expect(anyPending([doc("ready", 3), doc("pending")])).toBe(true);
    expect(anyPending([doc("ready", 3), doc("failed")])).toBe(false);
  });

  it("labels each document", () => {
    expect(documentStatus(doc("pending"))).toBe("indexing…");
    expect(documentStatus(doc("failed"))).toBe("failed");
    expect(documentStatus(doc("ready", 1))).toBe("1 chunk");
    expect(documentStatus(doc("ready", 1200))).toBe("1,200 chunks");
  });
});
