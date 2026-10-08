"""Turn a provider reply into (answer text, cited sources).

With web search on, Anthropic returns content blocks rather than a string: the
answer is split across ``text`` blocks, and each block that makes a claim from
a search result carries ``citations`` with that result's URL and title. Those
citations are the only trustworthy source list. URLs the model types into its
prose can be invented; URLs in citation metadata came back from the search.
"""

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage

from waypoint.agents.state import Finding, Source


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
            url = citation.get("url") if isinstance(citation, dict) else None
            if url and url not in seen:
                seen[url] = Source(url=url, title=str(citation.get("title") or url))
    return "".join(parts).strip(), list(seen.values())


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
