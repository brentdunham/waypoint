"""Parsing documents into sections (rag/parse.py)."""

import pytest

from waypoint.rag.parse import ParseError, media_type_for, parse, parse_html, parse_markdown

MARKDOWN = """---
title: ignored front matter
---
# Deploy

Intro paragraph that wraps
over two lines.

## 4. Caddy

Caddy terminates TLS.

```bash
# not a heading
docker compose up -d
```

- one
- two

| a | b |
|---|---|
| 1 | 2 |

---

### Reload `config`

Run **caddy reload**.

## Empty

## [Linked](https://example.com) heading

Last.
"""


def test_markdown_sections_follow_heading_paths() -> None:
    doc = parse_markdown(MARKDOWN, "DEPLOY.md")
    assert doc.title == "Deploy"
    paths = [s.path for s in doc.sections]
    # The single H1 is the title, so it is not repeated in every path, and a
    # heading with no text of its own ("Empty") yields no section.
    assert paths == [(), ("4. Caddy",), ("4. Caddy", "Reload config"), ("Linked heading",)]


def test_markdown_paragraphs_keep_code_lists_and_tables_whole() -> None:
    caddy = parse_markdown(MARKDOWN, "DEPLOY.md").sections[1]
    assert caddy.paragraphs[0] == "Caddy terminates TLS."
    assert caddy.paragraphs[1].startswith("```bash\n# not a heading")
    assert caddy.paragraphs[2] == "- one\n- two"
    assert caddy.paragraphs[3].splitlines()[0] == "| a | b |"
    assert len(caddy.paragraphs) == 4  # the --- rule is a separator, not text


def test_markdown_joins_wrapped_prose() -> None:
    intro = parse_markdown(MARKDOWN, "DEPLOY.md").sections[0]
    assert intro.paragraphs == ("Intro paragraph that wraps over two lines.",)


def test_markdown_with_several_h1s_keeps_them_in_paths() -> None:
    doc = parse_markdown("# A\n\ntext a\n\n# B\n\ntext b\n", "two.md")
    assert doc.title == "two.md"
    assert [s.path for s in doc.sections] == [("A",), ("B",)]


def test_an_unclosed_fence_keeps_its_text() -> None:
    doc = parse_markdown("## S\n\n```\ncode\n", "x.md")
    assert doc.sections[0].paragraphs == ("```\ncode",)


HTML = """<html><head><title>Guide</title><style>p{}</style></head><body>
<nav><a href="/">Home</a></nav>
<h1>Guide</h1>
<p>First   paragraph.</p>
<h2>Setup</h2>
<p>Install it.<br>Then run it.</p>
<ul><li>alpha</li><li>beta</li></ul>
<pre><code>line 1
line 2</code></pre>
<script>alert(1)</script>
<footer>copyright</footer>
</body></html>"""


def test_html_drops_chrome_and_splits_on_headings() -> None:
    doc = parse_html(HTML, "guide.html")
    assert doc.title == "Guide"
    assert [s.path for s in doc.sections] == [(), ("Setup",)]
    assert doc.sections[0].paragraphs == ("First paragraph.",)
    setup = doc.sections[1].paragraphs
    assert setup[:3] == ("Install it. Then run it.", "alpha", "beta")
    assert setup[3] == "line 1\nline 2"
    text = " ".join(p for s in doc.sections for p in s.paragraphs)
    for chrome in ("Home", "alert", "copyright"):
        assert chrome not in text


def _pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one line of Helvetica text per page."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_pdf_is_one_section_per_page() -> None:
    doc = parse(_pdf(["Hello page one", "Second page text"]), "application/pdf", "r.pdf")
    assert doc.title == "r.pdf"
    assert [s.path for s in doc.sections] == [("p. 1",), ("p. 2",)]
    assert "Hello page one" in doc.sections[0].paragraphs[0]


def test_a_pdf_without_text_is_an_error() -> None:
    with pytest.raises(ParseError, match="no extractable text"):
        parse(_pdf([""]), "application/pdf", "scan.pdf")


def test_garbage_pdf_is_an_error() -> None:
    with pytest.raises(ParseError):
        parse(b"definitely not a pdf", "application/pdf", "x.pdf")


def test_text_must_be_utf8() -> None:
    with pytest.raises(ParseError, match="UTF-8"):
        parse("café".encode("latin-1"), "text/markdown", "x.md")


def test_plain_text_is_paragraphs_without_headings() -> None:
    doc = parse(b"one\n\ntwo\r\n\r\nthree", "text/plain", "notes.txt")
    assert [s.paragraphs for s in doc.sections] == [("one", "two", "three")]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("docs/DEPLOY.md", "text/markdown"),
        ("a\\b\\page.HTM", "text/html"),
        ("r.pdf", "application/pdf"),
        ("x.docx", None),
        ("noext", None),
    ],
)
def test_media_type_by_extension(name: str, expected: str | None) -> None:
    assert media_type_for(name) == expected
