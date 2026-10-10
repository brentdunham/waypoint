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

from waypoint.errors import NotFound
from waypoint.infra.db import Database, SchemaMismatchError
from waypoint.rag.chunk import Chunk
from waypoint.rag.store import CorpusStore, NewDocument, tenant_transaction

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
            "scopes": ["agents:run", "threads:read", "threads:write"],
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
