"""``python -m waypoint.seed``: index ``RAG__SEED_DIR`` into the public corpus now.

The service does this in the background at every boot (``rag/seed.py``); this
runs it in the foreground, for a first deploy or to see what changed. Needs
``DATABASE_URL``, ``RAG__EMBEDDINGS_MODEL`` and ``VOYAGE_API_KEY``.

A module of its own, outside ``waypoint.rag``: ``import waypoint`` builds the
app factory, which imports ``rag/seed.py``, and running an already-imported
module as ``__main__`` is unreliable.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

from waypoint.agents.models import build_contextualizer, build_rag_embeddings
from waypoint.infra.db import Database
from waypoint.loops import selector_loop
from waypoint.rag.seed import describe, seed_public_corpus
from waypoint.settings import Settings


async def _main(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m waypoint.seed", description=__doc__)
    parser.add_argument("--dir", help="overrides RAG__SEED_DIR")
    parser.add_argument("--name", help="overrides RAG__SEED_CORPUS")
    opts = parser.parse_args(args)
    settings = Settings()
    rag = settings.rag.model_copy(
        update={k: v for k, v in {"seed_dir": opts.dir, "seed_corpus": opts.name}.items() if v}
    )
    settings = settings.model_copy(update={"rag": rag})
    embeddings = build_rag_embeddings(settings)
    if embeddings is None or settings.pg_dsn is None or not rag.seed_dir:
        print("needs RAG__EMBEDDINGS_MODEL, DATABASE_URL and RAG__SEED_DIR (or --dir)")
        return 2
    db = await Database(settings.pg_dsn, max_size=4).open()
    try:
        async with db.advisory_lock():
            await db.migrate(embedding_dims=rag.embeddings_dims)
        result = await seed_public_corpus(db, settings, embeddings, build_contextualizer(settings))
    finally:
        await db.close()
    if result is None:
        return 1
    print(describe(result, rag.seed_corpus))
    for failure in result.failed:
        print(f"  failed: {failure}")
    return 1 if result.failed else 0


if __name__ == "__main__":
    loop = selector_loop if os.name == "nt" else None
    sys.exit(asyncio.run(_main(sys.argv[1:]), loop_factory=loop))
