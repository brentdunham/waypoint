"""Corpora API, the ingest agent and the in-memory corpus store. Unit tier.

Offline: FAKE_MODEL swaps the Voyage embedder for HashEmbeddings, and with no
Postgres the runtime uses MemoryCorpusStore. Row-level security is covered by
the integration tier (test_rag_pg.py); here the same visibility rules are
checked on the in-memory store.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from tests.conftest import TestApp, build_settings, read_sse

from waypoint.agents.models import (
    EmbeddingsConfigError,
    HashEmbeddings,
    build_rag_embeddings,
    check_embedding_dims,
)
from waypoint.errors import Conflict, NotFound
from waypoint.rag.index import EmbeddingError, embed_with_retry
from waypoint.rag.store import MemoryCorpusStore, NewDocument

DOCS = Path(__file__).resolve().parents[1] / "docs"
RAG = {"embeddings_model": "voyageai:voyage-4", "embeddings_dims": 64}


@pytest.fixture
def rag_app(make_app: Any) -> TestApp:
    return make_app(rag=RAG)  # type: ignore[no-any-return]


@pytest.fixture
def rag(rag_app: TestApp) -> TestClient:
    return rag_app.test_client()


def corpus(client: TestClient, name: str = "waypoint-docs") -> str:
    response = client.post("/api/v1/corpora", json={"name": name})
    assert response.status_code == 201, response.json()
    return str(response.json()["corpus_id"])


def docs_files() -> list[tuple[str, tuple[str, bytes, str]]]:
    """This repository's docs/, named by their path below the repo root."""
    files = sorted(DOCS.rglob("*.md"))
    assert files, "docs/ should contain Markdown"
    return [
        ("files", (f"docs/{p.relative_to(DOCS).as_posix()}", p.read_bytes(), "text/markdown"))
        for p in files
    ]


def upload(client: TestClient, corpus_id: str, files: list[Any]) -> dict[str, Any]:
    response = client.post(f"/api/v1/corpora/{corpus_id}/documents", files=files)
    assert response.status_code == 202, response.json()
    body: dict[str, Any] = response.json()
    if body["run_id"]:
        assert read_sse(client, body["events_url"])[-1] == "run.completed"
    return body


# -- the API --------------------------------------------------------------------


def test_rag_off_means_503(client: TestClient) -> None:
    response = client.get("/api/v1/corpora")
    assert response.status_code == 503
    assert "RAG__EMBEDDINGS_MODEL" in response.json()["detail"]


def test_uploading_the_docs_folder_indexes_every_file(rag: TestClient) -> None:
    corpus_id = corpus(rag)
    files = docs_files()
    body = upload(rag, corpus_id, files)
    assert all(d["queued"] for d in body["documents"])

    run = rag.get(f"/api/v1/runs/{body['run_id']}").json()
    assert (run["agent"], run["status"]) == ("ingest", "completed")

    docs = rag.get(f"/api/v1/corpora/{corpus_id}/documents").json()["documents"]
    assert len(docs) == len(files)
    assert {d["status"] for d in docs} == {"ready"}, [d["error"] for d in docs]
    assert all(d["chunks"] > 0 for d in docs)
    titles = {d["title"] for d in docs}
    assert {"docs/DEPLOY.md", "docs/design/rag.md"} <= titles

    shown = rag.get(f"/api/v1/corpora/{corpus_id}").json()
    assert shown["documents"] == len(files)
    assert shown["chunks"] == sum(d["chunks"] for d in docs)
    assert shown["embed_model"] == "voyageai:voyage-4"


def test_ingest_reports_progress_and_a_summary(rag: TestClient) -> None:
    corpus_id = corpus(rag)
    files = [("files", ("a.md", b"# A\n\n## One\n\nalpha\n\n## Two\n\nbeta\n", "text/markdown"))]
    body = upload(rag, corpus_id, files)
    events: list[tuple[str, dict[str, Any]]] = []
    name = ""
    with rag.stream("GET", body["events_url"]) as response:
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((name, json.loads(line.removeprefix("data: "))))
                if name == "run.completed":
                    break
    stages = [data["payload"]["stage"] for kind, data in events if kind == "custom"]
    assert stages == ["parsed", "chunked", "embedded", "stored"]
    summary = events[-1][1]["result"]["metadata"]
    assert summary | {"results": []} == {
        "corpus_id": corpus_id,
        "documents": 1,
        "ready": 1,
        "failed": 0,
        "chunks": 2,
        "results": [],
    }


def test_reuploading_the_same_bytes_starts_no_run(rag: TestClient) -> None:
    corpus_id = corpus(rag)
    files = [("files", ("a.md", b"# A\n\nsame text\n", "text/markdown"))]
    upload(rag, corpus_id, files)
    again = upload(rag, corpus_id, files)
    assert again["run_id"] is None
    assert [d["queued"] for d in again["documents"]] == [False]
    assert again["documents"][0]["status"] == "ready"


def test_a_bad_file_fails_alone(rag: TestClient) -> None:
    corpus_id = corpus(rag)
    files = [
        ("files", ("good.md", b"# Good\n\ntext\n", "text/markdown")),
        ("files", ("bad.pdf", b"not a pdf at all", "application/pdf")),
    ]
    body = upload(rag, corpus_id, files)
    assert rag.get(f"/api/v1/runs/{body['run_id']}").json()["status"] == "completed"
    docs = {
        d["title"]: d for d in rag.get(f"/api/v1/corpora/{corpus_id}/documents").json()["documents"]
    }
    assert docs["good.md"]["status"] == "ready"
    assert docs["bad.pdf"]["status"] == "failed"
    assert "PDF" in docs["bad.pdf"]["error"]

    # Uploading the same failed bytes again retries them.
    retry = upload(rag, corpus_id, [files[1]])
    assert retry["documents"][0]["queued"] is True


def test_upload_validation(make_app: Any) -> None:
    client = make_app(rag=RAG | {"max_upload_mb": 0.001}).test_client()
    corpus_id = corpus(client)
    url = f"/api/v1/corpora/{corpus_id}/documents"
    unsupported = client.post(url, files=[("files", ("x.docx", b"data", "application/x"))])
    assert unsupported.status_code == 400
    assert ".md" in unsupported.json()["detail"]
    too_big = client.post(url, files=[("files", ("big.md", b"x" * 2000, "text/markdown"))])
    assert too_big.status_code == 413
    empty = client.post(url, files=[("files", ("e.md", b"  \n", "text/markdown"))])
    assert empty.status_code == 400
    missing = client.post(
        "/api/v1/corpora/not-a-uuid/documents",
        files=[("files", ("a.md", b"# A\n\nx\n", "text/markdown"))],
    )
    assert missing.status_code == 404


def test_corpus_lifecycle(rag: TestClient) -> None:
    corpus_id = corpus(rag, "notes")
    assert rag.post("/api/v1/corpora", json={"name": "notes"}).status_code == 409
    assert rag.post("/api/v1/corpora", json={"name": "bad/name"}).status_code == 422
    names = [c["name"] for c in rag.get("/api/v1/corpora").json()["corpora"]]
    assert names == ["notes"]
    assert rag.delete(f"/api/v1/corpora/{corpus_id}").status_code == 204
    assert rag.get(f"/api/v1/corpora/{corpus_id}").status_code == 404
    assert rag.delete(f"/api/v1/corpora/{corpus_id}").status_code == 404


def test_ingest_is_not_a_chat_agent(rag: TestClient) -> None:
    names = {a["name"] for a in rag.get("/api/v1/agents").json()["agents"]}
    assert "ingest" not in names
    assert rag.post("/api/v1/threads", json={"agent": "ingest"}).status_code == 400


# -- the in-memory store: the RLS rules, in Python ------------------------------


def test_memory_store_isolates_tenants() -> None:
    async def scenario() -> None:
        store = MemoryCorpusStore()
        mine = await store.create_corpus("acme", "c", "m")
        public = await store.create_corpus("seed", "demo", "m", visibility="public")
        doc = NewDocument("a.md", "text/markdown", b"# A\n\nx\n")
        [added] = await store.add_documents("acme", mine.corpus_id, [doc])

        # Another tenant sees the public corpus and nothing of acme's.
        assert [c.name for c in await store.list_corpora("evil")] == ["demo"]
        assert await store.get_corpus("evil", mine.corpus_id) is None
        assert await store.list_documents("evil", mine.corpus_id) == []
        assert await store.load_document("evil", added.document.document_id) is None
        assert not await store.delete_corpus("evil", mine.corpus_id)
        with pytest.raises(NotFound):
            await store.add_documents("evil", mine.corpus_id, [doc])
        with pytest.raises(NotFound):
            await store.write_chunks("evil", added.document.document_id, [], [])

        # Public corpora are readable by all and writable by none, owner included.
        assert await store.get_corpus("evil", public.corpus_id) is not None
        for tenant in ("evil", "seed"):
            with pytest.raises(NotFound):
                await store.add_documents(tenant, public.corpus_id, [doc])
            assert not await store.delete_corpus(tenant, public.corpus_id)

        with pytest.raises(Conflict):
            await store.create_corpus("acme", "c", "m")

    asyncio.run(scenario())


# -- embeddings -------------------------------------------------------------------


class _Flaky(HashEmbeddings):
    def __init__(self, failures: int, status: int | None = None) -> None:
        super().__init__(dims=4)
        self.failures, self.status, self.calls = failures, status, 0

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls <= self.failures:
            error = ConnectionError("provider blip")
            error.status_code = self.status  # type: ignore[attr-defined]
            raise error
        return self.embed_documents(texts)


def test_embedding_retries_transient_errors() -> None:
    flaky = _Flaky(failures=2)
    vectors = asyncio.run(embed_with_retry(flaky, ["a", "b"], backoff_s=0))
    assert (len(vectors), flaky.calls) == (2, 3)


def test_embedding_gives_up_after_the_last_attempt() -> None:
    flaky = _Flaky(failures=5)
    with pytest.raises(EmbeddingError, match="provider blip"):
        asyncio.run(embed_with_retry(flaky, ["a"], attempts=2, backoff_s=0))
    assert flaky.calls == 2


def test_a_rejected_key_is_not_retried() -> None:
    flaky = _Flaky(failures=5, status=401)
    with pytest.raises(EmbeddingError):
        asyncio.run(embed_with_retry(flaky, ["a"], backoff_s=0))
    assert flaky.calls == 1


class _VoyageAuthError(Exception):
    """Shaped like voyageai.error.AuthenticationError: the status is ``http_status``."""

    http_status = 401


class _RejectingEmbeddings(HashEmbeddings):
    async def aembed_query(self, text: str) -> list[float]:
        raise _VoyageAuthError("Provided API key is invalid.")


def test_a_rejected_voyage_key_stops_the_boot() -> None:
    with pytest.raises(EmbeddingsConfigError, match="401"):
        asyncio.run(check_embedding_dims(_RejectingEmbeddings(dims=4), 4))


def test_rag_embeddings_are_configured_separately_from_memory() -> None:
    assert build_rag_embeddings(build_settings()) is None
    fake = build_rag_embeddings(build_settings(rag=RAG))
    assert isinstance(fake, HashEmbeddings)
    assert fake.dims == 64


def test_voyage_needs_its_key() -> None:
    settings = build_settings(fake_model=False, rag={"embeddings_model": "voyageai:voyage-4"})
    with pytest.raises(EmbeddingsConfigError, match="VOYAGE_API_KEY"):
        build_rag_embeddings(settings)


def test_voyage_width_must_be_one_it_offers() -> None:
    settings = build_settings(
        fake_model=False,
        voyage_api_key="pa-test-not-real",
        rag={"embeddings_model": "voyageai:voyage-4", "embeddings_dims": 1536},
    )
    with pytest.raises(EmbeddingsConfigError, match="1024"):
        build_rag_embeddings(settings)


def test_voyage_is_asked_for_the_configured_width() -> None:
    settings = build_settings(
        fake_model=False,
        voyage_api_key="pa-test-not-real",
        rag={"embeddings_model": "voyageai:voyage-4", "embeddings_dims": 1024},
    )
    embeddings = build_rag_embeddings(settings)
    assert getattr(embeddings, "model", None) == "voyage-4"
    assert getattr(embeddings, "output_dimension", None) == 1024
