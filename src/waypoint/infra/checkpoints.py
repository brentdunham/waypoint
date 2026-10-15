"""Fence checkpoint writes as well as Redis output after worker takeover.

Each write locks the thread row and verifies the attempt token in the same
Postgres transaction as the checkpoint. Registering a successor takes that
same row lock and rechecks its Redis ownership, so a delayed predecessor
cannot register or commit writes after the successor has taken over.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, cast, override

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from waypoint.infra.db import Database
from waypoint.infra.queue import Claimed, OwnershipLostError, RunQueue

FENCE: ContextVar[tuple[str, str] | None] = ContextVar("checkpoint_fence", default=None)


@asynccontextmanager
async def checkpoint_fence(
    db: Database | None, queue: RunQueue, claimed: Claimed, consumer: str, lease_s: float
) -> AsyncIterator[None]:
    if db is None or claimed.owner is None:
        yield
        return
    thread_id = claimed.job.thread_id
    async with db.pool.connection() as conn, conn.transaction():
        cursor = await conn.execute(
            "select thread_id from threads where thread_id = %s for update", (thread_id,)
        )
        if await cursor.fetchone() is None or not await queue.renew(
            claimed, consumer, lease_s=lease_s
        ):
            raise OwnershipLostError
        await conn.execute(
            """insert into run_fences (thread_id, owner) values (%s, %s)
               on conflict (thread_id) do update set owner = excluded.owner""",
            (thread_id, claimed.owner),
        )
    token = FENCE.set((thread_id, claimed.owner))
    try:
        yield
    finally:
        FENCE.reset(token)


class FencedPostgresSaver(AsyncPostgresSaver):
    def __init__(self, db: Database) -> None:
        super().__init__(db.pool)  # type: ignore[arg-type]
        self._db = db

    @asynccontextmanager
    async def _writer(self) -> AsyncIterator[AsyncPostgresSaver]:
        fence = FENCE.get()
        if fence is None:
            yield AsyncPostgresSaver(self.conn, serde=self.serde)
            return
        thread_id, owner = fence
        async with self._db.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                """select f.owner from threads t left join run_fences f using (thread_id)
                   where t.thread_id = %s for update of t""",
                (thread_id,),
            )
            row = cast("dict[str, Any] | None", await cursor.fetchone())
            if row is None or row["owner"] != owner:
                raise OwnershipLostError
            yield AsyncPostgresSaver(conn, serde=self.serde)  # type: ignore[arg-type]

    @override
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        async with self._writer() as saver:
            return await saver.aput(config, checkpoint, metadata, new_versions)

    @override
    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        async with self._writer() as saver:
            await saver.aput_writes(config, writes, task_id, task_path)
