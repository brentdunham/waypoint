"""Seeding the public demo corpus (rag/seed.py). Unit tier: an in-memory owner
store and hash vectors. The Postgres version, with row-level security, is in
test_rag_pg.py."""

import asyncio
from pathlib import Path

from tests.conftest import build_settings

from waypoint.agents.models import HashEmbeddings
from waypoint.rag.seed import SYSTEM_TENANT, SeedResult, collect, describe, seed_corpus
from waypoint.rag.store import MemoryCorpusStore


def _tree(root: Path) -> Path:
    docs = root / "docs"
    (docs / "guide").mkdir(parents=True)
    (docs / "API.md").write_text("# API\n\nEvery endpoint.\n", encoding="utf-8")
    (docs / "guide" / "01-intro.md").write_text("# Intro\n\nHello.\n", encoding="utf-8")
    (docs / "diagram.png").write_bytes(b"\x89PNG")
    return docs


def test_collect_reads_parseable_files_titled_by_relative_path(tmp_path: Path) -> None:
    files = collect(_tree(tmp_path), tmp_path)
    assert sorted(files) == ["docs/API.md", "docs/guide/01-intro.md"]
    assert files["docs/API.md"].startswith(b"# API")


def test_seeding_reconciles_instead_of_rebuilding(tmp_path: Path) -> None:
    docs = _tree(tmp_path)
    store = MemoryCorpusStore(owner=True)
    embeddings = HashEmbeddings(dims=16)

    async def seed(model: str = "voyageai:voyage-4") -> SeedResult:
        return await seed_corpus(
            store,
            embeddings,
            name="waypoint-docs",
            files=collect(docs, tmp_path),
            embed_model=model,
            chunk_tokens=400,
        )

    async def scenario() -> None:
        first = await seed()
        assert first.indexed == ["docs/API.md", "docs/guide/01-intro.md"]
        [corpus] = await store.list_corpora(SYSTEM_TENANT)
        assert corpus.visibility == "public"
        assert corpus.name == "waypoint-docs"

        unchanged = await seed()
        assert unchanged.indexed == []
        assert unchanged.unchanged == 2
        assert unchanged.corpus_id == first.corpus_id

        (docs / "API.md").write_text("# API\n\nEvery endpoint, now with curl.\n", encoding="utf-8")
        (docs / "guide" / "01-intro.md").unlink()
        (docs / "NEW.md").write_text("# New\n\nA new page.\n", encoding="utf-8")
        changed = await seed()
        assert changed.indexed == ["docs/API.md", "docs/NEW.md"]
        assert changed.removed == ["docs/guide/01-intro.md"]
        docs_now = await store.list_documents(SYSTEM_TENANT, first.corpus_id)
        assert sorted(d.title for d in docs_now) == ["docs/API.md", "docs/NEW.md"]
        assert {d.status for d in docs_now} == {"ready"}
        assert "2 indexed, 0 unchanged, 1 removed" in describe(changed, "waypoint-docs")

        # Vectors from another model cannot be searched with these: start over.
        rebuilt = await seed("voyageai:voyage-4-large")
        assert rebuilt.corpus_id != first.corpus_id
        assert rebuilt.indexed == ["docs/API.md", "docs/NEW.md"]
        assert len(await store.list_corpora(SYSTEM_TENANT)) == 1

    asyncio.run(scenario())


def test_a_tenant_store_cannot_write_the_public_corpus(tmp_path: Path) -> None:
    store = MemoryCorpusStore()  # not an owner: what the API uses

    async def scenario() -> None:
        corpus = await store.create_corpus(SYSTEM_TENANT, "docs", "m", visibility="public")
        assert not await store.delete_corpus(SYSTEM_TENANT, corpus.corpus_id)

    asyncio.run(scenario())


def test_a_blank_seed_dir_means_no_seeding() -> None:
    settings = build_settings(rag={"embeddings_model": "voyageai:voyage-4", "seed_dir": " "})
    assert settings.rag.seed_dir is None
    assert settings.rag.seed_corpus == "waypoint-docs"
