"""Reviewers' own corpora and the daily upload limits (docs/design/tools.md, T13). Unit tier.

With auth off every request is the anonymous principal, whose ``*`` scope
makes it an admin, and admins are not limited. The reviewer tests therefore
override authentication with a principal that has a reviewer key's scopes.
The Redis implementation of the counters is covered by the integration tier
(test_rag_pg.py).
"""

import asyncio
import datetime as dt
from typing import Any

import pytest
from starlette.testclient import TestClient
from tests.conftest import TestApp, new_thread, read_sse, run_and_wait

from waypoint.errors import OverQuota
from waypoint.infra.quota import MB, MemoryUploadQuota
from waypoint.infra.security import Principal, authenticate
from waypoint.rag.store import NewDocument
from waypoint.settings import UploadSettings

RAG = {"embeddings_model": "voyageai:voyage-4", "embeddings_dims": 64}
REVIEWER = Principal(
    tenant_id="rev-acme",
    user_id="reviewer",
    key_id="k1",
    scopes=frozenset({"agents:run", "threads:read", "threads:write", "corpora:write"}),
)
TWO_CHUNKS = b"# A\n\n## One\n\nalpha\n\n## Two\n\nbeta\n"


def reviewer_app(make_app: Any, **uploads: Any) -> TestApp:
    app: TestApp = make_app(rag=RAG, uploads=uploads)
    app.app.dependency_overrides[authenticate] = lambda: REVIEWER
    return app


def md(name: str, body: bytes) -> tuple[str, tuple[str, bytes, str]]:
    return ("files", (name, body, "text/markdown"))


def new_corpus(client: TestClient, name: str = "mine") -> str:
    response = client.post("/api/v1/corpora", json={"name": name})
    assert response.status_code == 201, response.json()
    return str(response.json()["corpus_id"])


def send(client: TestClient, corpus_id: str, files: list[Any]) -> Any:
    response = client.post(f"/api/v1/corpora/{corpus_id}/documents", files=files)
    if response.status_code == 202 and response.json()["run_id"]:
        assert read_sse(client, response.json()["events_url"])[-1] == "run.completed"
    return response


# -- the counters ------------------------------------------------------------------


class Clock(MemoryUploadQuota):
    def __init__(self, settings: UploadSettings, now: dt.datetime) -> None:
        super().__init__(settings)
        self.now = now

    async def _now(self) -> dt.datetime:
        return self.now


def test_a_refused_upload_counts_nothing() -> None:
    async def check() -> None:
        quota = Clock(
            UploadSettings(files_per_day=3, mb_per_day=1),
            dt.datetime(2026, 10, 14, 18, tzinfo=dt.UTC),
        )
        await quota.take_files("t", 2, 1000)
        with pytest.raises(OverQuota) as refused:
            await quota.take_files("t", 2, 1000)
        assert refused.value.extra["limit"] == "files"
        assert refused.value.extra["retry_after"] == 6 * 3600  # midnight UTC
        assert "3 files a day; 2 are used today" in refused.value.detail
        with pytest.raises(OverQuota) as too_big:
            await quota.take_files("t", 1, MB)
        assert too_big.value.extra["limit"] == "bytes"
        used = await quota.usage("t")
        assert (used.files, used.bytes) == (2, 1000)  # neither refusal took anything
        assert used.resets_at == "2026-10-15T00:00:00+00:00"

    asyncio.run(check())


def test_giving_back_and_the_next_day() -> None:
    async def check() -> None:
        quota = Clock(UploadSettings(files_per_day=2), dt.datetime(2026, 10, 14, 23, tzinfo=dt.UTC))
        await quota.take_files("t", 2, 10)
        await quota.give_back_files("t", 1, 5)
        await quota.take_files("t", 1, 5)  # the duplicate's place is free again
        await quota.give_back_files("t", 5, 500)
        assert (await quota.usage("t")).files == 0  # never below zero
        await quota.take_files("t", 2, 10)
        quota.now += dt.timedelta(hours=2)
        await quota.take_files("t", 2, 10)  # a new UTC day
        assert (await quota.usage("other")).files == 0  # per tenant

    asyncio.run(check())


def test_chunks_are_all_or_nothing() -> None:
    async def check() -> None:
        quota = MemoryUploadQuota(UploadSettings(chunks_per_day=10))
        await quota.take_chunks("t", 8)
        with pytest.raises(OverQuota, match="This document has 3 chunks"):
            await quota.take_chunks("t", 3)
        await quota.take_chunks("t", 2)
        await quota.give_back_chunks("t", 4)
        assert (await quota.usage("t")).chunks == 6

    asyncio.run(check())


# -- the API, as a reviewer ----------------------------------------------------------


def test_a_reviewer_owns_at_most_max_corpora(make_app: Any) -> None:
    client = reviewer_app(make_app, max_corpora=2).test_client()
    new_corpus(client, "a")
    new_corpus(client, "b")
    response = client.post("/api/v1/corpora", json={"name": "c"})
    assert response.status_code == 409
    assert response.json()["max_corpora"] == 2
    assert "Delete one first" in response.json()["detail"]


def test_an_admin_is_not_capped(make_app: Any) -> None:
    client = make_app(rag=RAG, uploads={"max_corpora": 1}).test_client()
    new_corpus(client, "a")
    new_corpus(client, "b")


def test_the_eleventh_file_gets_a_clean_429(make_app: Any) -> None:
    client = reviewer_app(make_app, files_per_day=2).test_client()
    corpus_id = new_corpus(client)
    assert send(client, corpus_id, [md("a.md", b"# A\n\none\n")]).status_code == 202
    again = send(client, corpus_id, [md("a.md", b"# A\n\none\n")])  # a duplicate: given back
    assert again.status_code == 202
    assert again.json()["run_id"] is None
    assert send(client, corpus_id, [md("b.md", b"# B\n\ntwo\n")]).status_code == 202
    refused = send(client, corpus_id, [md("c.md", b"# C\n\nthree\n")])
    assert refused.status_code == 429
    assert refused.json()["type"].endswith("/over-quota")
    assert refused.json()["limit"] == "files"
    assert 0 < int(refused.headers["Retry-After"]) <= 86_400
    docs = client.get(f"/api/v1/corpora/{corpus_id}/documents").json()["documents"]
    assert sorted(d["title"] for d in docs) == ["a.md", "b.md"]  # nothing of c.md stored


def test_megabytes_per_day(make_app: Any) -> None:
    client = reviewer_app(make_app, mb_per_day=0.001).test_client()  # 1,048 bytes
    corpus_id = new_corpus(client)
    body = b"# A\n\n" + b"word " * 300
    refused = send(client, corpus_id, [md("big.md", body)])
    assert refused.status_code == 429
    assert refused.json()["limit"] == "bytes"


def test_a_document_over_the_chunk_limit_fails_alone(make_app: Any) -> None:
    client = reviewer_app(make_app, chunks_per_day=3).test_client()
    corpus_id = new_corpus(client)
    response = send(client, corpus_id, [md("a.md", TWO_CHUNKS), md("b.md", TWO_CHUNKS + b"x\n")])
    assert response.status_code == 202
    docs = client.get(f"/api/v1/corpora/{corpus_id}/documents").json()["documents"]
    assert sorted(d["status"] for d in docs) == ["failed", "ready"]
    [failed] = [d for d in docs if d["status"] == "failed"]
    assert "chunks" in failed["error"]
    assert "midnight UTC" in failed["error"]
    usage = client.get("/api/v1/corpora/usage").json()
    assert usage["chunks"] == 2  # the failed document's chunks were never taken


def test_usage_reports_the_limits(make_app: Any) -> None:
    client = reviewer_app(make_app).test_client()
    corpus_id = new_corpus(client)
    send(client, corpus_id, [md("a.md", TWO_CHUNKS)])
    usage = client.get("/api/v1/corpora/usage").json()
    assert usage | {"resets_at": None} == {
        "limited": True,
        "corpora": 1,
        "max_corpora": 3,
        "files": 1,
        "files_per_day": 10,
        "bytes": len(TWO_CHUNKS),
        "bytes_per_day": 5 * MB,
        "chunks": 2,
        "chunks_per_day": 2000,
        "resets_at": None,
    }


def test_admin_uploads_are_not_limited(make_app: Any) -> None:
    client = make_app(rag=RAG, uploads={"files_per_day": 1, "chunks_per_day": 1}).test_client()
    corpus_id = new_corpus(client)
    response = send(client, corpus_id, [md("a.md", TWO_CHUNKS), md("b.md", TWO_CHUNKS + b"x")])
    assert response.status_code == 202
    docs = client.get(f"/api/v1/corpora/{corpus_id}/documents").json()["documents"]
    assert {d["status"] for d in docs} == {"ready"}
    assert client.get("/api/v1/corpora/usage").json()["limited"] is False


# -- the tool ------------------------------------------------------------------------


def test_the_tool_says_when_documents_are_still_indexing(make_app: Any) -> None:
    app: TestApp = make_app(rag=RAG)
    client = app.test_client()
    corpus_id = new_corpus(client, "fresh")
    # Stored but not ingested: the state an upload is in for its first seconds.
    doc = NewDocument(title="new.md", media_type="text/markdown", content=b"# New\n\ntext\n")
    app.call(app.runtime.corpora.add_documents, "public", corpus_id, [doc])
    thread_id = new_thread(client, "workspace")
    message = '[[tool:search_documents({"query": "new"})]] What is new?'
    body = {"message": message, "input": {"corpus_ids": [corpus_id]}}
    assert run_and_wait(client, thread_id, body)["status"] == "completed"
    values = client.get(f"/api/v1/threads/{thread_id}/state").json()["values"]
    tool = next(m for m in values["messages"] if m.get("type") == "tool")
    assert '1 document is still being indexed in "fresh"' in tool["content"]
