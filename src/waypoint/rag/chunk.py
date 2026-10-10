"""Sections to chunks (design decision R4). Pure functions, no I/O.

A chunk is the unit of retrieval and of a ``search_result`` block: a heading
path for its title, and paragraphs that become that block's text blocks. The
rules, in order:

* A chunk never crosses a section boundary, so its heading is always true.
* Paragraphs are packed until the next would pass ``target_tokens``.
* The next chunk of the same section starts with the previous chunk's last
  paragraph (one-paragraph overlap), when that leaves room for new text.
* A paragraph bigger than the target on its own is split: code at line
  breaks, prose at sentence ends, and as a last resort at the target length.

Token counts are estimated as characters / 4. Good enough to size chunks; the
retrieval eval, not this estimate, decides what size works (phase 4).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from waypoint.rag.parse import Section

HEADING_SEPARATOR = " \u203a "  # a single right-pointing angle quote, as in a breadcrumb
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    """Position in the document, from 0."""
    heading: str
    """The document label, then the section's headings, joined by ``HEADING_SEPARATOR``."""
    paragraphs: tuple[str, ...]
    context: str = ""
    """Contextual-retrieval preamble (``rag/contextualize.py``), empty when off.
    Searched and embedded, never shown to the model that answers."""

    @property
    def text(self) -> str:
        """What gets embedded: context, then the heading, so a section's name counts
        as content, then the paragraphs."""
        return "\n\n".join(
            (*([self.context] if self.context else []), self.heading, *self.paragraphs)
        )


def estimate_tokens(text: str) -> int:
    return max(1, -(-len(text) // CHARS_PER_TOKEN))


def heading_for(label: str, path: Iterable[str]) -> str:
    return HEADING_SEPARATOR.join((label, *path))


def chunk_sections(sections: Iterable[Section], label: str, *, target_tokens: int) -> list[Chunk]:
    """Chunks for a whole document, numbered in reading order."""
    chunks: list[Chunk] = []
    for section in sections:
        heading = heading_for(label, section.path)
        for paragraphs in _pack(section.paragraphs, target_tokens):
            chunks.append(Chunk(len(chunks), heading, paragraphs))
    return chunks


def _pack(paragraphs: Iterable[str], target: int) -> list[tuple[str, ...]]:
    pieces = [piece for p in paragraphs for piece in split_paragraph(p, target)]
    out: list[tuple[str, ...]] = []
    current: list[str] = []
    size = 0
    fresh = False  # does `current` hold anything beyond the carried-over overlap?
    for piece in pieces:
        tokens = estimate_tokens(piece)
        if current and size + tokens > target:
            if fresh:
                out.append(tuple(current))
            overlap = current[-1]
            overlap_tokens = estimate_tokens(overlap)
            if fresh and overlap_tokens + tokens <= target:
                current, size = [overlap], overlap_tokens
            else:
                current, size = [], 0
            fresh = False
        current.append(piece)
        size += tokens
        fresh = True
    if current and fresh:
        out.append(tuple(current))
    return out


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9`*])")


def split_paragraph(paragraph: str, target: int) -> list[str]:
    """``paragraph`` as is if it fits, else pieces that each fit."""
    if estimate_tokens(paragraph) <= target:
        return [paragraph]
    code = paragraph.startswith(("```", "~~~")) or "\n" in paragraph
    units = paragraph.split("\n") if code else _SENTENCE_END.split(paragraph)
    joiner = "\n" if code else " "
    pieces: list[str] = []
    current = ""
    for unit in units:
        for part in _hard_split(unit, target):
            candidate = f"{current}{joiner}{part}" if current else part
            if current and estimate_tokens(candidate) > target:
                pieces.append(current)
                current = part
            else:
                current = candidate
    if current:
        pieces.append(current)
    return pieces


def _hard_split(text: str, target: int) -> list[str]:
    """Cut text that has no usable boundary (a minified line, a URL list)."""
    limit = target * CHARS_PER_TOKEN
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [text]
