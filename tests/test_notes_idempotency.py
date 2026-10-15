"""The mutation and its replay result must be committed together."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from waypoint.mcp_demo.notes import MemoryNotes, SqliteNotes


@pytest.mark.parametrize("sqlite", [False, True])
def test_repeated_writes_keep_the_first_result_and_are_tenant_scoped(
    tmp_path: Path, sqlite: bool
) -> None:
    store = SqliteNotes(tmp_path / "notes.sqlite") if sqlite else MemoryNotes()
    with ThreadPoolExecutor(max_workers=4) as pool:
        notes = list(pool.map(lambda _: store.add("alice", "one note", "thread:call"), range(8)))
    assert len({n.note_id for n in notes}) == 1
    assert len(store.all("alice")) == 1
    assert store.add("bob", "other note", "thread:call").text == "other note"
    if sqlite:
        store = SqliteNotes(tmp_path / "notes.sqlite")
    assert store.add("alice", "one note", "thread:call") == notes[0]
    assert store.delete("alice", notes[0].note_id, "thread:delete")
    assert store.delete("alice", notes[0].note_id, "thread:delete")
    assert not store.all("alice")
    with pytest.raises(ValueError, match="different operation"):
        store.add("alice", "different payload", "thread:call")
