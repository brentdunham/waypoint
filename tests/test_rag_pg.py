"""Tenant isolation for corpora in real Postgres: design decision R5.

Two guards are tested separately. The store's own ``where tenant_id = ...``
clauses are one; row-level security is the other, so the RLS tests go around
the store and issue raw SQL as the ``waypoint_tenant`` role. The last test
shows why that role exists: the service's own user owns the tables (and is a
superuser in the compose images), so it bypasses RLS entirely.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient
from tests.conftest import (
    BOOTSTRAP_KEY,
    TEST_DATABASE_URL,
    TEST_REDIS_URL,
    TestApp,
    read_sse,
    requires_infra,
)

from waypoint.agents.models import HashEmbeddings
from waypoint.errors import NotFound
from waypoint.infra.db import Database, SchemaMismatchError
from waypoint.rag.chunk import Chunk
from waypoint.rag.seed import SYSTEM_TENANT, collect, seed_corpus
from waypoint.rag.store import CorpusStore, NewDocument, PgCorpusStore, tenant_transaction

pytestmark = [pytest.mark.integration, requires_infra]

DIMS = 1024
RAG = {"embeddings_model": "voyageai:voyage-4", "embeddings_dims": DIMS}


@pytest.fixture
def pg_app(make_app: Any) -> TestApp:
    return make_app(  # type: ignore[no-any-return]
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        auth_enabled=True,
        bootstrap_api_key=BOOTSTRAP_KEY,
        limits={"rpm": 1000, "concurrent_runs": 50},
        rag=RAG,
    )


def tenant() -> str:
    return f"t-{uuid.uuid4().hex[:12]}"


def run(app: TestApp, scenario: Callable[[CorpusStore, Database], Awaitable[None]]) -> None:
    async def go() -> None:
        assert app.runtime.db is not None
        await scenario(app.runtime.corpora, app.runtime.db)

    app.call(go)


async def seeded(store: CorpusStore, owner: str) -> tuple[str, str]:
    """A private corpus with one indexed document. Returns (corpus_id, document_id)."""
    corpus = await store.create_corpus(owner, f"c-{uuid.uuid4().hex[:8]}", "voyageai:voyage-4")
    [added] = await store.add_documents(
        owner, corpus.corpus_id, [NewDocument("a.md", "text/markdown", b"# A\n\nsecret\n")]
    )
    doc_id = added.document.document_id
    await store.write_chunks(owner, doc_id, [Chunk(0, "a.md", ("secret",))], [[0.1] * DIMS])
    return corpus.corpus_id, doc_id


async def as_tenant(db: Database, tenant_id: str | None, sql: str, params: Any = ()) -> Any:
    """Raw SQL as the tenant role, with ``app.tenant_id`` set (or not, for None)."""
    async with tenant_transaction(db, tenant_id) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall() if cur.description else None


def test_the_store_hides_one_tenants_corpora_from_another(pg_app: TestApp) -> None:
    acme, evil = tenant(), tenant()

    async def scenario(store: CorpusStore, db: Database) -> None:
        corpus_id, doc_id = await seeded(store, acme)
        assert [c.corpus_id for c in await store.list_corpora(acme)] == [corpus_id]
        assert (await store.get_corpus(acme, corpus_id)).chunks == 1  # type: ignore[union-attr]

        assert all(c.corpus_id != corpus_id for c in await store.list_corpora(evil))
        assert await store.get_corpus(evil, corpus_id) is None
        assert await store.list_documents(evil, corpus_id) == []
        assert await store.list_chunks(evil, corpus_id) == []
        assert await store.load_document(evil, doc_id) is None
        assert not await store.delete_corpus(evil, corpus_id)
        with pytest.raises(NotFound):
            await store.add_documents(evil, corpus_id, [NewDocument("x.md", "text/markdown", b"x")])
        with pytest.raises(NotFound):
            await store.write_chunks(evil, doc_id, [], [])
        assert len(await store.list_chunks(acme, corpus_id)) == 1  # untouched

    run(pg_app, scenario)


def test_rls_returns_nothing_without_the_tenant_setting(pg_app: TestApp) -> None:
    acme = tenant()

    async def scenario(store: CorpusStore, db: Database) -> None:
        corpus_id, _ = await seeded(store, acme)
        count = "select count(*) as n from {} where corpus_id = %s"
        for table in ("corpora", "documents", "chunks"):
            # No app.tenant_id at all, and the wrong one: the policy filters
            # every row even though the query names the corpus directly.
            for who in (None, tenant()):
                rows = await as_tenant(db, who, count.format(table), (corpus_id,))
                assert rows[0]["n"] == 0, (table, who)
            rows = await as_tenant(db, acme, count.format(table), (corpus_id,))
            assert rows[0]["n"] == 1, table

    run(pg_app, scenario)


def test_rls_refuses_writes_into_another_tenants_corpus(pg_app: TestApp) -> None:
    acme, evil = tenant(), tenant()

    async def scenario(store: CorpusStore, db: Database) -> None:
        corpus_id, doc_id = await seeded(store, acme)
        insert = """insert into documents (document_id, corpus_id, tenant_id, title, media_type,
                                           content, sha256)
                    values (%s, %s, %s, 'x', 'text/plain', 'x', %s)"""
        # The foreign key alone would accept this row: it names a real corpus.
        for claimed in (evil, acme):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await as_tenant(
                    db, evil, insert, (str(uuid.uuid4()), corpus_id, claimed, uuid.uuid4().hex)
                )
        # Updates and deletes of someone else's rows match nothing.
        await as_tenant(db, evil, "delete from chunks where document_id = %s", (doc_id,))
        rename = "update documents set title = 'pwned' where document_id = %s"
        await as_tenant(db, evil, rename, (doc_id,))
        assert len(await store.list_chunks(acme, corpus_id)) == 1
        assert (await store.list_documents(acme, corpus_id))[0].title == "a.md"

    run(pg_app, scenario)


def test_public_corpora_are_readable_by_all_and_writable_by_none(pg_app: TestApp) -> None:
    seed, other = tenant(), tenant()

    async def scenario(store: CorpusStore, db: Database) -> None:
        corpus_id, _ = await seeded(store, seed)
        # Only the owner role (migrations, seeding) can publish a corpus.
        publish = "update corpora set visibility = 'public' where corpus_id = %s"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await as_tenant(db, seed, publish, (corpus_id,))
        await db.execute(publish, (corpus_id,))

        assert await store.get_corpus(other, corpus_id) is not None
        assert len(await store.list_chunks(other, corpus_id)) == 1
        for who in (other, seed):
            assert not await store.delete_corpus(who, corpus_id)
            with pytest.raises(NotFound):
                await store.add_documents(who, corpus_id, [NewDocument("y.md", "text/plain", b"y")])

    run(pg_app, scenario)


def test_the_service_user_bypasses_rls_which_is_why_the_role_exists(pg_app: TestApp) -> None:
    acme = tenant()

    async def scenario(store: CorpusStore, db: Database) -> None:
        corpus_id, _ = await seeded(store, acme)
        row = await db.fetch_one(
            "select count(*) as n from chunks where corpus_id = %s", (corpus_id,)
        )
        assert row is not None
        assert row["n"] == 1  # no role, no setting: still visible

    run(pg_app, scenario)


def test_migrations_rerun_and_the_width_is_checked(pg_app: TestApp) -> None:
    async def scenario(store: CorpusStore, db: Database) -> None:
        await db.migrate(embedding_dims=DIMS)  # idempotent, policies recreated
        assert await db.embedding_dims() == DIMS
        await db.check_embedding_dims(DIMS)
        with pytest.raises(SchemaMismatchError, match=f"RAG__EMBEDDINGS_DIMS={DIMS}"):
            await db.check_embedding_dims(512)

    run(pg_app, scenario)


def test_vector_and_keyword_search_in_postgres(pg_app: TestApp) -> None:
    acme, evil = tenant(), tenant()

    def unit(i: int) -> list[float]:
        return [1.0 if j == i else 0.0 for j in range(DIMS)]

    async def scenario(store: CorpusStore, db: Database) -> None:
        corpus = await store.create_corpus(acme, f"s-{uuid.uuid4().hex[:8]}", "m")
        [added] = await store.add_documents(
            acme, corpus.corpus_id, [NewDocument("cfg.md", "text/markdown", b"x")]
        )
        texts = [
            "Caddy terminates TLS for the demo host.",
            "Set STREAM__BLOCK_MS below the socket timeout, or idle streams fail.",
            "Reconnecting clients resume from the last event id.",
        ]
        chunks = [Chunk(i, f"cfg.md > {i}", (t,)) for i, t in enumerate(texts)]
        await store.write_chunks(
            acme, added.document.document_id, chunks, [unit(i) for i in range(3)]
        )
        cid = corpus.corpus_id

        near = await store.vector_search(acme, cid, unit(2), 2)
        assert len(near) == 2
        assert near[0].chunk.ordinal == 2  # the other two tie at similarity 0
        assert near[0].score == pytest.approx(1.0)

        # Identifiers survive the English parser, and OR-ed terms match a
        # question that shares only some of its words with the chunk.
        ident = await store.keyword_search(acme, cid, "what is STREAM__BLOCK_MS for?", 5)
        assert [h.chunk.ordinal for h in ident] == [1]
        question = await store.keyword_search(acme, cid, "how do clients reconnect?", 5)
        assert question[0].chunk.ordinal == 2
        assert await store.keyword_search(acme, cid, "the and of", 5) == []  # stopwords only

        assert await store.vector_search(evil, cid, unit(2), 5) == []
        assert await store.keyword_search(evil, cid, "Caddy", 5) == []

    run(pg_app, scenario)


def test_uploading_docs_through_the_api_is_invisible_to_another_tenant(
    pg_app: TestApp, auth: dict[str, str]
) -> None:
    from tests.test_corpora import docs_files

    client: TestClient = pg_app.test_client()
    name = f"waypoint-docs-{uuid.uuid4().hex[:8]}"
    created = client.post("/api/v1/corpora", json={"name": name}, headers=auth)
    assert created.status_code == 201, created.json()
    corpus_id = created.json()["corpus_id"]

    files = docs_files()
    response = client.post(f"/api/v1/corpora/{corpus_id}/documents", files=files, headers=auth)
    assert response.status_code == 202, response.json()
    assert read_sse(client, response.json()["events_url"], auth)[-1] == "run.completed"
    docs = client.get(f"/api/v1/corpora/{corpus_id}/documents", headers=auth).json()["documents"]
    assert len(docs) == len(files)
    assert {d["status"] for d in docs} == {"ready"}, [d["error"] for d in docs]

    minted = client.post(
        "/api/v1/keys",
        json={
            "tenant_id": tenant(),
            "user_id": "u",
            "scopes": ["agents:run", "threads:read", "threads:write", "corpora:write"],
        },
        headers=auth,
    ).json()["api_key"]
    other = {"Authorization": f"Bearer {minted}"}
    listed = client.get("/api/v1/corpora", headers=other).json()["corpora"]
    assert all(c["corpus_id"] != corpus_id for c in listed)
    for method, url in (
        ("GET", f"/api/v1/corpora/{corpus_id}"),
        ("GET", f"/api/v1/corpora/{corpus_id}/documents"),
        ("DELETE", f"/api/v1/corpora/{corpus_id}"),
    ):
        assert client.request(method, url, headers=other).status_code == 404, url
    upload = client.post(f"/api/v1/corpora/{corpus_id}/documents", files=files[:1], headers=other)
    assert upload.status_code == 404


def test_changing_corpora_needs_the_corpora_write_scope(pg_app: TestApp) -> None:
    auth = {"Authorization": f"Bearer {BOOTSTRAP_KEY}"}
    client: TestClient = pg_app.test_client()

    def key(scopes: list[str]) -> dict[str, str]:
        body = {"tenant_id": tenant(), "user_id": "u", "scopes": scopes}
        raw = client.post("/api/v1/keys", json=body, headers=auth).json()["api_key"]
        return {"Authorization": f"Bearer {raw}"}

    reviewer = key(["agents:run", "threads:read", "threads:write"])  # deploy/reviewer-link.sh
    assert client.get("/api/v1/corpora", headers=reviewer).status_code == 200
    denied = client.post("/api/v1/corpora", json={"name": "mine"}, headers=reviewer)
    assert denied.status_code == 403
    assert "corpora:write" in denied.json()["detail"]

    owner = key(["agents:run", "threads:read", "threads:write", "corpora:write"])
    created = client.post("/api/v1/corpora", json={"name": "mine"}, headers=owner)
    assert created.status_code == 201
    corpus_id = created.json()["corpus_id"]
    files = [("files", ("a.md", b"# A\n\ntext\n", "text/markdown"))]
    upload = client.post(f"/api/v1/corpora/{corpus_id}/documents", files=files, headers=reviewer)
    assert upload.status_code == 403
    assert client.delete(f"/api/v1/corpora/{corpus_id}", headers=reviewer).status_code == 403
    assert client.delete(f"/api/v1/corpora/{corpus_id}", headers=owner).status_code == 204


def test_seeding_keeps_a_public_corpus_in_step_with_a_directory(
    pg_app: TestApp, tmp_path: Any
) -> None:
    name = f"seed-{uuid.uuid4().hex[:8]}"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nalpha text here\n", encoding="utf-8")
    (docs / "b.md").write_text("# B\n\nbeta text here\n", encoding="utf-8")
    (docs / "logo.png").write_bytes(b"not a document")

    async def scenario(store: CorpusStore, db: Database) -> None:
        owner = PgCorpusStore(db, owner=True)
        embeddings = HashEmbeddings(DIMS)

        async def seed() -> Any:
            return await seed_corpus(
                owner,
                embeddings,
                name=name,
                files=collect(docs, tmp_path),
                embed_model="voyageai:voyage-4",
                chunk_tokens=400,
            )

        first = await seed()
        assert first.indexed == ["docs/a.md", "docs/b.md"]
        assert not first.failed
        visitor = tenant()
        [public] = [c for c in await store.list_corpora(visitor) if c.corpus_id == first.corpus_id]
        assert public.visibility == "public"
        assert public.documents == 2
        # Readable by everyone, changeable by no tenant: not even the system
        # tenant's name through the tenant role.
        for who in (visitor, SYSTEM_TENANT):
            assert not await store.delete_corpus(who, first.corpus_id)
            with pytest.raises(NotFound):
                await store.add_documents(
                    who, first.corpus_id, [NewDocument("x.md", "text/markdown", b"x")]
                )

        again = await seed()
        assert again.corpus_id == first.corpus_id
        assert again.indexed == []
        assert again.unchanged == 2

        (docs / "a.md").write_text("# A\n\nalpha, changed\n", encoding="utf-8")
        (docs / "b.md").unlink()
        third = await seed()
        assert third.indexed == ["docs/a.md"]
        assert third.removed == ["docs/b.md"]
        titles = [d.title for d in await store.list_documents(visitor, first.corpus_id)]
        assert titles == ["docs/a.md"]
        assert await owner.delete_corpus(SYSTEM_TENANT, first.corpus_id)

    run(pg_app, scenario)


def test_only_an_admin_publishes_and_only_their_own_corpora(pg_app: TestApp) -> None:
    """T12 (docs/design/tools.md): global corpora are published by an admin.

    The store publishes as the tables' owner, so the tenant role still never
    writes a public row; the API checks ``keys:admin`` first.
    """
    auth = {"Authorization": f"Bearer {BOOTSTRAP_KEY}"}
    client: TestClient = pg_app.test_client()

    def key(scopes: list[str], tenant_id: str) -> dict[str, str]:
        body = {"tenant_id": tenant_id, "user_id": "u", "scopes": scopes}
        raw = client.post("/api/v1/keys", json=body, headers=auth).json()["api_key"]
        return {"Authorization": f"Bearer {raw}"}

    admin_tenant, reviewer_tenant = tenant(), tenant()
    admin = key(["*"], admin_tenant)
    writer = key(["agents:run", "threads:read", "threads:write", "corpora:write"], reviewer_tenant)
    mine = client.post("/api/v1/corpora", json={"name": "handbook"}, headers=admin).json()
    theirs = client.post("/api/v1/corpora", json={"name": "notes"}, headers=writer).json()
    public = {"visibility": "public"}

    denied = client.post(
        f"/api/v1/corpora/{theirs['corpus_id']}/visibility", json=public, headers=writer
    )
    assert denied.status_code == 403
    other = client.post(
        f"/api/v1/corpora/{theirs['corpus_id']}/visibility", json=public, headers=admin
    )
    assert other.status_code == 404  # an admin publishes only their own tenant's corpora

    published = client.post(
        f"/api/v1/corpora/{mine['corpus_id']}/visibility", json=public, headers=admin
    )
    assert published.status_code == 200
    listed = client.get("/api/v1/corpora", headers=writer).json()["corpora"]
    assert any(c["corpus_id"] == mine["corpus_id"] and c["visibility"] == "public" for c in listed)
    # Still unwritable through the API while public: unpublish first.
    files = [("files", ("a.md", b"# A\n\ntext\n", "text/markdown"))]
    upload = client.post(
        f"/api/v1/corpora/{mine['corpus_id']}/documents", files=files, headers=admin
    )
    assert upload.status_code == 404


def test_a_reviewer_uploads_within_the_daily_limits_in_redis(make_app: Any) -> None:
    """Phase 2b end to end on real infrastructure (docs/design/tools.md, T13).

    The counters are the Redis ones here, so this is where the Lua script's
    all-or-nothing take and the 429's ``Retry-After`` are checked.
    """
    app: TestApp = make_app(
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        auth_enabled=True,
        bootstrap_api_key=BOOTSTRAP_KEY,
        limits={"rpm": 1000, "concurrent_runs": 50},
        rag=RAG,
        uploads={"max_corpora": 1, "files_per_day": 2, "chunks_per_day": 3},
    )
    client = app.test_client()
    auth = {"Authorization": f"Bearer {BOOTSTRAP_KEY}"}
    body = {
        "tenant_id": tenant(),
        "user_id": "reviewer",
        "scopes": ["agents:run", "threads:read", "threads:write", "corpora:write"],
    }
    raw = client.post("/api/v1/keys", json=body, headers=auth).json()["api_key"]
    reviewer = {"Authorization": f"Bearer {raw}"}

    created = client.post("/api/v1/corpora", json={"name": "mine"}, headers=reviewer)
    assert created.status_code == 201
    corpus_id = created.json()["corpus_id"]
    assert client.post("/api/v1/corpora", json={"name": "two"}, headers=reviewer).status_code == 409

    two_chunks = b"# A\n\n## One\n\nalpha\n\n## Two\n\nbeta\n"
    files = [
        ("files", ("a.md", two_chunks, "text/markdown")),
        ("files", ("b.md", two_chunks + b"x\n", "text/markdown")),
    ]
    url = f"/api/v1/corpora/{corpus_id}/documents"
    accepted = client.post(url, files=files, headers=reviewer)
    assert accepted.status_code == 202, accepted.json()
    assert read_sse(client, accepted.json()["events_url"], reviewer)[-1] == "run.completed"
    docs = client.get(url, headers=reviewer).json()["documents"]
    assert sorted(d["status"] for d in docs) == ["failed", "ready"]  # 2 + 2 chunks > 3

    third = [("files", ("c.md", b"# C\n\nthree\n", "text/markdown"))]
    refused = client.post(url, files=third, headers=reviewer)
    assert refused.status_code == 429
    assert refused.json()["limit"] == "files"
    assert 0 < int(refused.headers["Retry-After"]) <= 86_400
    usage = client.get("/api/v1/corpora/usage", headers=reviewer).json()
    assert (usage["files"], usage["chunks"], usage["limited"]) == (2, 2, True)
