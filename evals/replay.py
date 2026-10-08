"""Record every model call an eval makes, and replay it when the call recurs.

The research searcher is most of an eval's bill: web search results go back
in as input on every later search a worker makes. Most experiments change one
role (a writer prompt, the critic) and should not pay to search again. So each
model call is stored under a key made of exactly what it was given:

    case id, trial, agent, role, model,
    the bound call (tools and ``max_uses``, output schema, ``max_tokens``),
    the input messages (ids dropped, today's date masked).

A call whose key is stored is answered from the store. Anything that changes
a call's input or setup changes its key, so the change runs live and so does
everything downstream of it: edit the critic prompt and the critic runs
again, its new follow-up questions are searched, and the writer, which now
sees different findings, writes again. The planner and the first wave of
searches replay. ``--fresh`` ignores stored answers and records new ones.

Replayed calls are not billed and are not in a scorecard's token counts;
``usage.replayed`` says how many there were per role. The trial is part of the
key, so three trials replay three independent samples, not one sample three
times.
"""

import datetime as dt
import hashlib
import importlib
import json
import sqlite3
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

from langchain_core._api import LangChainBetaWarning
from langchain_core.language_models import BaseChatModel
from langchain_core.load import dumpd, load
from langchain_core.load.serializable import Serializable
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable, RunnableBinding, RunnableConfig, RunnableLambda
from pydantic import BaseModel

FILENAME = "replay.sqlite"
"""Kept beside the results (``--out``), which is gitignored and, in the eval
container, the one writable mount."""


class ReplayStore:
    """A key -> JSON table in SQLite. One connection, used from one event loop."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS calls ("
            " key TEXT PRIMARY KEY, role TEXT, model TEXT, value TEXT, recorded TEXT)"
        )
        self._db.commit()

    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM calls WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, role: str, model: str, value: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO calls VALUES (?, ?, ?, ?, ?)",
            (key, role, model, value, dt.datetime.now(dt.UTC).isoformat(timespec="seconds")),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


class Replay:
    """One case and trial's view of the store. Implements ``CallInterceptor``."""

    def __init__(self, store: ReplayStore, case_id: str, trial: int, *, read: bool = True) -> None:
        self.store = store
        self.case_id = case_id
        self.trial = trial
        self.read = read
        self.replayed: Counter[str] = Counter()

    def wrap(self, agent: str, role: str, model: str, runnable: Any) -> Any:
        signature = _signature(runnable)

        def key_for(value: Any) -> str:
            blob = json.dumps(
                [self.case_id, self.trial, agent, role, model, signature, _input_key(value)],
                sort_keys=True,
                ensure_ascii=False,
                default=repr,
            )
            return hashlib.sha256(blob.encode()).hexdigest()

        def lookup(key: str) -> tuple[bool, Any]:
            if not self.read:
                return False, None
            stored = self.store.get(key)
            if stored is None:
                return False, None
            self.replayed[role] += 1
            return True, decode(json.loads(stored))

        def record(key: str, out: Any) -> None:
            encoded = encode(out)
            if encoded is not None:
                self.store.put(key, role, model, json.dumps(encoded, ensure_ascii=False))

        def call(value: Any, config: RunnableConfig) -> Any:
            key = key_for(value)
            hit, out = lookup(key)
            if not hit:
                out = runnable.invoke(value, config)
                record(key, out)
            return out

        async def acall(value: Any, config: RunnableConfig) -> Any:
            key = key_for(value)
            hit, out = lookup(key)
            if not hit:
                out = await runnable.ainvoke(value, config)
                record(key, out)
            return out

        return RunnableLambda(call, afunc=acall, name=f"replay:{role}")


# -- keys ----------------------------------------------------------------------------


def _signature(runnable: Runnable[Any, Any]) -> str:
    """What the call is bound to: tool definitions, output schema, model params.

    Walks the runnable for ``RunnableBinding`` kwargs and each chat model's
    ``_get_llm_string`` (model, max_tokens, temperature; secrets are references,
    never values).
    """
    parts: list[Any] = []

    def walk(r: Any) -> None:
        if isinstance(r, RunnableBinding):
            parts.append(r.kwargs)
        if isinstance(r, BaseChatModel):
            parts.append(r._get_llm_string())
        for attr in ("bound", "first", "last"):
            if (child := getattr(r, attr, None)) is not None:
                walk(child)
        for child in getattr(r, "middle", None) or []:
            walk(child)

    walk(runnable)
    return json.dumps(parts, sort_keys=True, ensure_ascii=False, default=repr)


_VOLATILE = {"id", "usage_metadata", "response_metadata", "additional_kwargs"}


def _input_key(value: Any) -> Any:
    """The call's input without what differs between identical runs."""
    if hasattr(value, "to_messages"):
        value = value.to_messages()
    if isinstance(value, list):
        out: Any = [_message_key(m) if isinstance(m, BaseMessage) else m for m in value]
    else:
        out = value
    text = json.dumps(out, sort_keys=True, ensure_ascii=False, default=repr)
    return text.replace(dt.date.today().isoformat(), "<today>")


def _message_key(message: BaseMessage) -> dict[str, Any]:
    data = message.model_dump()
    for field in _VOLATILE:
        data.pop(field, None)
    for call in data.get("tool_calls") or []:
        call.pop("id", None)
    data.pop("tool_call_id", None)
    return data


# -- values ----------------------------------------------------------------------------


def encode(value: Any) -> dict[str, Any] | None:
    """JSON for a message or one of our pydantic outputs; None if neither."""
    if isinstance(value, Serializable) and value.is_lc_serializable():
        return {"lc": dumpd(value)}
    if isinstance(value, BaseModel):
        cls = type(value)
        return {"model": f"{cls.__module__}:{cls.__qualname__}", "data": value.model_dump()}
    return None


def decode(stored: dict[str, Any]) -> Any:
    if "lc" in stored:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LangChainBetaWarning)  # `load` is marked beta
            return load(stored["lc"], allowed_objects="messages")
    module, _, name = stored["model"].partition(":")
    if not module.startswith("waypoint."):
        raise ValueError(f"replay store names a class outside waypoint: {stored['model']}")
    cls = getattr(importlib.import_module(module), name)
    return cls.model_validate(stored["data"])
