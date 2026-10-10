"""Documents to sections: a heading path and the paragraphs under it.

Parsing keeps structure and nothing else. Chunk sizes are not decided here
(that is ``chunk.py``), so the same parse serves every chunking experiment.

A paragraph is the unit a citation can point at: the research worker sends
each one as its own text block, and the API cites whole blocks. So a
paragraph is whatever a reader would quote as one piece: a prose paragraph, a
list, a table, a fenced code block.
"""

import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}
"""What ingestion accepts, by file extension. Anything else is refused at upload."""


class ParseError(ValueError):
    """The bytes are not a readable document of the declared type."""


@dataclass(frozen=True, slots=True)
class Section:
    path: tuple[str, ...]
    """Headings from the document's top to this section; empty before the first heading."""
    paragraphs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    title: str
    sections: list[Section] = field(default_factory=list)

    @property
    def paragraph_count(self) -> int:
        return sum(len(s.paragraphs) for s in self.sections)


def media_type_for(filename: str) -> str | None:
    return MEDIA_TYPES.get(PurePosixPath(filename.replace("\\", "/")).suffix.lower())


def parse(content: bytes, media_type: str, title: str) -> ParsedDocument:
    """Dispatch on media type. ``title`` is the fallback when the document has none."""
    if media_type == "application/pdf":
        return parse_pdf(content, title)
    text = _decode(content)
    if media_type == "text/markdown":
        return parse_markdown(text, title)
    if media_type == "text/html":
        return parse_html(text, title)
    if media_type == "text/plain":
        return ParsedDocument(title, _sections([((), _paragraphs(text))]))
    raise ParseError(f"Unsupported media type {media_type!r}.")


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("Text documents must be UTF-8.") from exc


def _paragraphs(text: str) -> list[str]:
    """Blank-line separated blocks, inner whitespace runs collapsed."""
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    return [b for b in (_squash(block) for block in blocks) if b]


def _squash(text: str) -> str:
    return " ".join(text.split())


def _sections(raw: list[tuple[tuple[str, ...], list[str]]]) -> list[Section]:
    """Drop empty sections (a heading straight followed by a subheading)."""
    return [Section(path, tuple(paras)) for path, paras in raw if paras]


# -- Markdown -------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")


def parse_markdown(text: str, title: str) -> ParsedDocument:
    """ATX headings (``#``) split sections; blank lines split paragraphs.

    Fenced code is one paragraph, kept verbatim, and a ``#`` inside it is not
    a heading. Lines of a list or table stay together, joined by newlines, so
    they read as the list or table they were. A document with exactly one H1
    uses it as the title, and it is left out of every heading path: the source
    list then reads ``DEPLOY.md > 4. Caddy`` rather than repeating the title.
    Setext headings (underlined with ``===``) are not recognised.
    """
    text = _FRONT_MATTER.sub("", text.replace("\r\n", "\n"))
    lines = text.split("\n")
    h1s = [h for level, h in _headings(lines) if level == 1]
    drop_h1 = len(h1s) == 1

    raw: list[tuple[tuple[str, ...], list[str]]] = [((), [])]
    stack: list[tuple[int, str]] = []
    block: list[str] = []
    fence: str | None = None

    def flush() -> None:
        if block:
            raw[-1][1].append(_markdown_block(block))
            block.clear()

    for line in lines:
        if fence is not None:
            block.append(line)
            if line.strip().startswith(fence):
                fence = None
                flush()
            continue
        if m := _FENCE.match(line):
            flush()
            fence = m.group(1)
            block.append(line)
            continue
        if m := _HEADING.match(line):
            flush()
            level, heading = len(m.group(1)), _strip_inline(m.group(2))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            path = tuple(h for lvl, h in stack if not (drop_h1 and lvl == 1))
            raw.append((path, []))
            continue
        if not line.strip() or _RULE.match(line):
            flush()
            continue
        block.append(line)
    flush()  # an unclosed fence keeps what it has

    return ParsedDocument(_strip_inline(h1s[0]) if drop_h1 else title, _sections(raw))


def _headings(lines: list[str]) -> list[tuple[int, str]]:
    """``(level, text)`` of every heading outside fenced code."""
    out: list[tuple[int, str]] = []
    fence: str | None = None
    for line in lines:
        if fence is not None:
            if line.strip().startswith(fence):
                fence = None
        elif m := _FENCE.match(line):
            fence = m.group(1)
        elif m := _HEADING.match(line):
            out.append((len(m.group(1)), m.group(2)))
    return out


def _markdown_block(lines: list[str]) -> str:
    if _FENCE.match(lines[0]):
        return "\n".join(lines).strip()
    structured = any(_LIST_ITEM.match(line) or line.lstrip().startswith("|") for line in lines)
    if structured:
        return "\n".join(line.rstrip() for line in lines).strip()
    return _squash(" ".join(lines))


def _strip_inline(heading: str) -> str:
    """``**Bold** `code` [link](x)`` -> ``Bold code link``: headings are labels."""
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    return re.sub(r"[*_`]", "", heading).strip()


# -- HTML -----------------------------------------------------------------------

_BLOCKS = frozenset({"p", "li", "pre", "blockquote", "td", "th", "dd", "dt", "figcaption"})
_CONTAINERS = frozenset({"div", "section", "article", "main", "ul", "ol", "table", "tr"})
_SKIP = frozenset({"script", "style", "nav", "header", "footer", "noscript", "svg", "template"})
_HEADINGS = {f"h{i}": i for i in range(1, 7)}


class _HtmlSections(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.raw: list[tuple[tuple[str, ...], list[str]]] = [((), [])]
        self._stack: list[tuple[int, str]] = []
        self._text: list[str] = []
        self._skip = 0
        self._in_title = False
        self._heading: int | None = None
        self._pre = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "br":
            self._text.append("\n" if self._pre else " ")
        elif tag in _HEADINGS or tag in _BLOCKS or tag in _CONTAINERS:
            if self._pre:
                return  # markup inside <pre> is part of the code block
            self._flush()
            if tag in _HEADINGS:
                self._heading = _HEADINGS[tag]
            if tag == "pre":
                self._pre += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _HEADINGS and self._heading is not None:
            heading = _squash("".join(self._text))
            self._text.clear()
            level, self._heading = self._heading, None
            if heading:
                while self._stack and self._stack[-1][0] >= level:
                    self._stack.pop()
                self._stack.append((level, heading))
                self.raw.append((tuple(h for _, h in self._stack), []))
        elif tag == "pre":
            self._flush()
            self._pre = max(0, self._pre - 1)
        elif (tag in _BLOCKS or tag in _CONTAINERS) and not self._pre:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self._text.append(data)

    def _flush(self) -> None:
        if self._heading is not None:
            return  # text inside a heading is the heading
        raw = "".join(self._text)
        text = raw.strip("\n") if self._pre else _squash(raw)
        if text.strip():
            self.raw[-1][1].append(text)
        self._text.clear()

    def close(self) -> None:
        super().close()
        self._flush()


def parse_html(text: str, title: str) -> ParsedDocument:
    """Headings ``h1``-``h6`` split sections; block elements are paragraphs.

    Scripts, styles and page chrome (``nav``, ``header``, ``footer``) are
    dropped. The ``<title>`` becomes the title, with ``title`` as fallback. A
    single ``h1`` is treated like Markdown's: it names the document and is
    left out of the heading paths.
    """
    parser = _HtmlSections()
    parser.feed(text)
    parser.close()
    sections = _sections(parser.raw)
    page_title = _squash(parser.title)
    h1s = {s.path[0] for s in sections if s.path}
    if len(h1s) == 1 and all(s.path for s in sections):
        h1 = next(iter(h1s))
        page_title = page_title or h1
        sections = [Section(s.path[1:], s.paragraphs) for s in sections]
    return ParsedDocument(page_title or title, sections)


# -- PDF ------------------------------------------------------------------------


def parse_pdf(content: bytes, title: str) -> ParsedDocument:
    """One section per page (``p. 3``), paragraphs split on blank lines.

    Text only, as the design's non-goals say: images and table structure are
    lost. A page with no extractable text (a scan) contributes nothing; a PDF
    with no text at all is a parse error, so the document fails visibly.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
        meta_title = (reader.metadata.title if reader.metadata else None) or ""
    except (PdfReadError, ValueError, KeyError, OSError) as exc:
        raise ParseError(f"Not a readable PDF: {exc}") from exc
    sections = _sections([((f"p. {n}",), _paragraphs(text)) for n, text in enumerate(pages, 1)])
    if not sections:
        raise ParseError("The PDF has no extractable text (scanned pages are not supported).")
    return ParsedDocument(_squash(meta_title) or title, sections)
