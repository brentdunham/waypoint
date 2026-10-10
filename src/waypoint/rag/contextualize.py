"""Contextual retrieval: a short preamble per chunk, written by a model.

Anthropic's recipe (https://www.anthropic.com/news/contextual-retrieval): show
a cheap model the whole document and one chunk, ask for a sentence or two
that situates the chunk in the document, and prepend that to the chunk before
embedding and keyword indexing. A chunk that says "the timeout must exceed it"
then also says which timeout, in which service.

One call per chunk. The document goes first in its own content block marked
for prompt caching, so every chunk after the first reads it from the cache.
The preamble is stored in ``chunks.context``: it shapes the vector and the
full-text index, and the research worker never sees it.

No import from ``waypoint.agents``: the caller builds the model, which keeps
this module free of the import cycle ``agents -> graphs -> rag`` would close.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from waypoint.rag.chunk import Chunk

DOCUMENT_PROMPT = "<document>\n{document}\n</document>"
CHUNK_PROMPT = """Here is the chunk we want to situate within the whole document
<chunk>
{chunk}
</chunk>
Please give a short succinct context to situate this chunk within the overall document for the purposes of improving search retrieval of the chunk. Answer only with the succinct context and nothing else."""
"""Anthropic's published prompt, unchanged, so a result here is comparable with theirs."""

MAX_TOKENS = 200
CONCURRENCY = 4
"""Calls in flight per document; the eval indexes four documents at a time."""


class ChatModel(Protocol):
    async def ainvoke(self, input: Any, /) -> Any: ...


@dataclass(slots=True)
class Usage:
    """Tokens spent writing preambles, for the eval's cost line."""

    calls: int = 0
    input_tokens: int = 0
    """Uncached input, as the API counts it."""
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    def add(self, metadata: dict[str, Any] | None) -> None:
        self.calls += 1
        if not metadata:
            return
        details = metadata.get("input_token_details") or {}
        read = int(details.get("cache_read") or 0)
        # langchain-anthropic reports writes per TTL when the API does, and then
        # zeroes the generic key. This module only writes the 5-minute cache.
        write = int(details.get("cache_creation") or 0) + sum(
            int(details.get(k) or 0)
            for k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        )
        self.cache_read_tokens += read
        self.cache_write_tokens += write
        # langchain's input_tokens is the total, cached parts included.
        self.input_tokens += max(0, int(metadata.get("input_tokens") or 0) - read - write)
        self.output_tokens += int(metadata.get("output_tokens") or 0)


@dataclass(slots=True)
class Contextualizer:
    """Writes the preamble for every chunk of one document at a time."""

    model: ChatModel
    model_name: str
    concurrency: int = CONCURRENCY
    usage: Usage = field(default_factory=Usage)

    async def __call__(self, document: str, chunks: Sequence[Chunk]) -> list[Chunk]:
        gate = asyncio.Semaphore(self.concurrency)
        doc_block = {
            "type": "text",
            "text": DOCUMENT_PROMPT.format(document=document),
            "cache_control": {"type": "ephemeral"},
        }

        async def one(chunk: Chunk) -> Chunk:
            body = "\n\n".join((chunk.heading, *chunk.paragraphs))
            message = {
                "role": "user",
                "content": [doc_block, {"type": "text", "text": CHUNK_PROMPT.format(chunk=body)}],
            }
            async with gate:
                reply = await self.model.ainvoke([message])
            self.usage.add(getattr(reply, "usage_metadata", None))
            return replace(chunk, context=_text(reply).strip())

        # The first call writes the cache; the rest can then read it.
        if not chunks:
            return []
        first = await one(chunks[0])
        rest = await asyncio.gather(*(one(c) for c in chunks[1:]))
        return [first, *rest]


def _text(reply: Any) -> str:
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    return "".join(
        str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def document_text(chunks: Sequence[Chunk]) -> str:
    """The document as the model sees it: each section's heading, then its text.

    Rebuilt from the chunks rather than the raw upload, so it is the parsed
    text (no HTML tags, no PDF layout noise). Overlapping paragraphs, which
    start the next chunk of a section, are written once.
    """
    out: list[str] = []
    last_heading = None
    seen_last: str | None = None
    for chunk in chunks:
        paragraphs = list(chunk.paragraphs)
        if chunk.heading == last_heading and paragraphs and paragraphs[0] == seen_last:
            paragraphs = paragraphs[1:]
        if chunk.heading != last_heading:
            out.append(chunk.heading)
            last_heading = chunk.heading
        out.extend(paragraphs)
        seen_last = chunk.paragraphs[-1] if chunk.paragraphs else None
    return "\n\n".join(out)
