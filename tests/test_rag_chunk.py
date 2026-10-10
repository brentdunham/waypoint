"""Chunking sections (rag/chunk.py): design decision R4."""

from itertools import pairwise

from waypoint.rag.chunk import (
    HEADING_SEPARATOR,
    chunk_sections,
    estimate_tokens,
    split_paragraph,
)
from waypoint.rag.parse import Section


def para(tokens: int, tag: str) -> str:
    """A paragraph estimated at exactly ``tokens`` tokens."""
    return (tag + " " + "x" * (tokens * 4))[: tokens * 4]


def test_packs_paragraphs_up_to_the_target() -> None:
    section = Section(("S",), tuple(para(30, f"p{i}") for i in range(10)))
    chunks = chunk_sections([section], "doc.md", target_tokens=100)
    assert all(sum(estimate_tokens(p) for p in c.paragraphs) <= 100 for c in chunks)
    assert len(chunks) > 1


def test_consecutive_chunks_share_one_paragraph() -> None:
    paragraphs = tuple(para(30, f"p{i}") for i in range(10))
    chunks = chunk_sections([Section(("S",), paragraphs)], "doc.md", target_tokens=100)
    for before, after in pairwise(chunks):
        assert after.paragraphs[0] == before.paragraphs[-1]
    # Nothing lost: every paragraph appears, in order.
    seen = list(dict.fromkeys(p for c in chunks for p in c.paragraphs))
    assert seen == list(paragraphs)


def test_no_overlap_when_it_would_not_leave_room() -> None:
    paragraphs = (para(80, "a"), para(80, "b"))
    chunks = chunk_sections([Section((), paragraphs)], "doc.md", target_tokens=100)
    assert [c.paragraphs for c in chunks] == [(paragraphs[0],), (paragraphs[1],)]


def test_chunks_never_cross_sections_and_carry_the_heading_path() -> None:
    sections = [
        Section((), ("intro",)),
        Section(("4. Caddy",), ("tls",)),
        Section(("4. Caddy", "Reload"), ("reload",)),
    ]
    chunks = chunk_sections(sections, "DEPLOY.md", target_tokens=400)
    sep = HEADING_SEPARATOR
    assert [(c.ordinal, c.heading, c.paragraphs) for c in chunks] == [
        (0, "DEPLOY.md", ("intro",)),
        (1, f"DEPLOY.md{sep}4. Caddy", ("tls",)),
        (2, f"DEPLOY.md{sep}4. Caddy{sep}Reload", ("reload",)),
    ]
    assert chunks[1].text == f"DEPLOY.md{sep}4. Caddy\n\ntls"


def test_an_oversized_paragraph_splits_at_sentences() -> None:
    sentence = "This sentence is about forty characters. "
    paragraph = (sentence * 40).strip()
    pieces = split_paragraph(paragraph, target=50)
    assert len(pieces) > 1
    assert all(estimate_tokens(p) <= 50 for p in pieces)
    assert all(p.endswith(".") for p in pieces)
    assert " ".join(pieces) == paragraph


def test_oversized_code_splits_at_lines() -> None:
    code = "```\n" + "\n".join(f"line {i:03d} " + "y" * 30 for i in range(50)) + "\n```"
    pieces = split_paragraph(code, target=60)
    assert len(pieces) > 1
    assert "\n".join(pieces) == code


def test_text_without_boundaries_is_cut_at_the_target() -> None:
    pieces = split_paragraph("z" * 1000, target=50)
    assert [len(p) for p in pieces] == [200, 200, 200, 200, 200]


def test_empty_sections_make_no_chunks() -> None:
    assert chunk_sections([Section(("x",), ())], "d", target_tokens=100) == []
