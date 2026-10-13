"""Turn a provider reply into (answer text, cited sources).

With web search on, Anthropic returns content blocks rather than a string: the
answer is split across ``text`` blocks, and each block that makes a claim from
a search result carries ``citations`` with that result's URL and title. Those
citations are the only trustworthy source list. URLs the model types into its
prose can be invented; URLs in citation metadata came back from the search.

Passages from the tenant's documents go in as ``search_result`` blocks and
come back cited as ``search_result_location``: the same shape, keyed by the
``source`` we gave the block instead of a URL. Both kinds share one numbering.
"""

import re
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage

from waypoint.agents.state import Finding, Source
from waypoint.rag.retrieve import Hit

MAX_QUOTES = 3
"""Quotes kept per source. The judge reads them to check attribution, so they
are bounded: a few short excerpts, not the page."""
MAX_QUOTE_CHARS = 400


def answer_and_sources(message: BaseMessage) -> tuple[str, list[Source]]:
    content: Any = message.content
    if isinstance(content, str):
        return content.strip(), []

    parts: list[str] = []
    seen: dict[str, Source] = {}
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict) or block.get("type") != "text":
            continue  # server_tool_use, web_search_tool_result: not the answer
        parts.append(str(block.get("text", "")))
        for citation in block.get("citations") or []:
            if not isinstance(citation, dict):
                continue
            if citation.get("type") == "search_result_location":
                key = citation.get("source")
                if not key:
                    continue
                source = seen.setdefault(
                    key,
                    Source(url=key, title=str(citation.get("title") or key), kind="document"),
                )
            else:
                url = citation.get("url")
                if not url:
                    continue
                source = seen.setdefault(
                    url, Source(url=url, title=str(citation.get("title") or url))
                )
            add_quote(source, citation.get("cited_text"))
    return "".join(parts).strip(), list(seen.values())


_TYPED_MARKERS = re.compile(r"[ \t]*\(?(?:\[\d+(?:-\d+)?\])+\)?")


def strip_typed_markers(answer: str) -> str:
    """A worker answer without citation marks the model typed into its prose.

    Workers are told not to type them; sources come only from citation
    metadata. When a searcher writes ``[2-1]`` anyway, the mark points at
    nothing, and the writer copied such marks into a briefing on 2026-10-10,
    where they failed ``citations_resolve``. Not for briefings: their ``[n]``
    marks are the real ones.
    """
    return _TYPED_MARKERS.sub("", answer)


def is_document(source: Source) -> bool:
    return source.get("kind") == "document"


def add_quote(source: Source, text: Any) -> None:
    quote = " ".join(str(text or "").split())
    if not quote:
        return
    if len(quote) > MAX_QUOTE_CHARS:
        quote = quote[: MAX_QUOTE_CHARS - 1].rstrip() + "…"
    quotes = source.setdefault("quotes", [])
    if quote not in quotes and len(quotes) < MAX_QUOTES:
        quotes.append(quote)


def retrieved_sources(message: BaseMessage, limit: int) -> list[Source]:
    """The pages the reply's own web searches returned, in result order.

    The fallback for a reply that searched but attached no citations: the
    URLs are real search results, but nothing ties a sentence to one of them.
    claude-haiku-5-5 did this in about 1 finding in 4 on 2026-10-08.
    """
    content: Any = message.content
    if isinstance(content, str):
        return []
    seen: dict[str, Source] = {}
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "web_search_tool_result":
            continue
        results = block.get("content")
        if not isinstance(results, list):
            continue  # an error result
        for result in results:
            url = result.get("url") if isinstance(result, dict) else None
            if url and url not in seen:
                seen[url] = Source(url=url, title=str(result.get("title") or url))
                if len(seen) >= limit:
                    return list(seen.values())
    return list(seen.values())


def number_sources(findings: Sequence[Finding]) -> dict[str, int]:
    """One global numbering across all findings, in first-cited order."""
    numbers: dict[str, int] = {}
    for finding in findings:
        for source in finding["sources"]:
            numbers.setdefault(source["url"], len(numbers) + 1)
    return numbers


CITATION = re.compile(r"\[(\d+)\]")
_BOUNDARY = re.compile(r"[.!?][\"')]*((?:\s*\[\d+\])*)\s+(?=[A-Z0-9\"'(])")
_TRAILING = re.compile(r"((?:\s*\[\d+\])*)\s*([.!?:;]?[\"')]*)$")


def split_sentences(paragraph: str) -> list[str]:
    """A paragraph's sentences. Citations written after the full stop
    ("... rose 4%. [3] Next ...") stay with the sentence they follow."""
    text = paragraph.strip()
    sentences: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        end = match.end(1) if match.group(1) else match.start(1)
        sentences.append(text[start:end].strip())
        start = match.end()
    if text[start:].strip():
        sentences.append(text[start:].strip())
    return [s for s in sentences if s]


def cite(sentence: str, numbers: Sequence[int]) -> str:
    """``sentence`` with its citations replaced by ``numbers``, before the final
    punctuation, the way the writer places them: "... rose 4% [2][5]."
    """
    bare = CITATION.sub("", sentence)
    bare = re.sub(r"\s+([.!?:;,])", r"\1", bare).rstrip()
    match = _TRAILING.search(bare)
    end = match.group(2) if match else ""
    stem = bare[: len(bare) - len(end)].rstrip() if end else bare
    marks = "".join(f"[{n}]" for n in dict.fromkeys(numbers))
    return f"{stem} {marks}{end}" if marks else f"{stem}{end}"


# -- retrieved passages (moved from graphs/research.py, 2026-10-13) ---------------


def search_result(hit: Hit) -> dict[str, Any]:
    """A retrieved chunk as a Claude ``search_result`` content block.

    One text block per paragraph: the API cites whole blocks, so this is what
    lets a citation be narrower than the chunk. Citations must be enabled on
    every search result in a request that also has the web search tool.
    """
    return {
        "type": "search_result",
        "source": hit.source,
        "title": hit.chunk.heading,
        "content": [{"type": "text", "text": p} for p in hit.chunk.paragraphs],
        "citations": {"enabled": True},
    }


def passage_source(hit: Hit) -> Source:
    """A retrieved passage as an uncited source, for the no-citations fallback."""
    return Source(url=hit.source, title=hit.chunk.heading, kind="document")
