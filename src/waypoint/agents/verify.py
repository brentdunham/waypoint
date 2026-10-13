"""Check a cited answer's citations against the evidence, and fix what is wrong.

Shared by the research agent (its ``verify`` step) and the workspace agent
(every reply that cites documents). Moved here from ``graphs/research.py`` on
2026-10-13 without changing a prompt, a schema or a decision, so research
briefings, replay keys and eval fingerprints are unchanged: ``research.py``
imports the prompt constants back, and the fingerprint reads them there.

One structured call to the ``verifier`` role lists the sentences that are not
supported as cited. Wrong or missing citations are fixed in code. Sentences no
evidence supports go to the reviser (the agent's writing model) once, and a
rewrite is kept only if every number it cites is a real source.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

from waypoint.agents.citations import CITATION, cite, split_sentences
from waypoint.agents.state import Finding

log = logging.getLogger(__name__)


class SentenceCheck(BaseModel):
    index: int = Field(description="The sentence's index, copied from the input.")
    verdict: Literal["wrong_source", "needs_citation", "unsupported"]
    sources: list[int] = Field(
        default_factory=list,
        description="For wrong_source and needs_citation: the source numbers that support "
        "the sentence, best first, at most three. Otherwise empty.",
    )
    note: str = Field(default="", description="For unsupported: what no finding supports.")


class Verification(BaseModel):
    """Only the sentences that need a change.

    The first version asked for a verdict on every sentence. On a 40-sentence
    briefing Haiku 5.5's reasoning plus 40 verdicts ran past ``max_tokens`` and
    returned empty or truncated JSON in 4 of 7 eval cases. Most sentences are
    fine, so listing only the exceptions is most of the saving.
    """

    issues: list[SentenceCheck] = Field(
        description="One entry per sentence that is not supported as cited. "
        "Empty when every sentence is."
    )


class Rewrite(BaseModel):
    index: int = Field(description="The sentence's index, copied from the input.")
    text: str = Field(
        default="",
        description="The sentence rewritten to say only what the findings support, with "
        "[n] citations. Empty to delete the sentence.",
    )


class Revision(BaseModel):
    """Replacements for the flagged sentences, spliced in by code.

    The first version asked the writer for the whole revised briefing. In the
    eval it returned only the rewritten sentences, and coverage fell from 4 to 1.
    Asking for replacements means a revision cannot touch anything else.
    """

    rewrites: list[Rewrite]


# -- prompts -----------------------------------------------------------------

VERIFY = """Check the citations in a research briefing against the evidence.

{today}

FINDINGS lists what the research found. SOURCES lists every source with its
number n, the finding it belongs to, and quotes: text from that page the
finding drew on. Quotes are excerpts, not the whole page. A source marked
retrieved_only was returned by a search but tied to no claim; prefer others.

Read every numbered briefing sentence. A sentence is fine when its claims are
in the cited sources' findings and the cited sources are the right ones, or
when it makes no factual claim. List only the sentences that are not, each
with one verdict:
- wrong_source: the claims are supported, but by other sources than the ones
  cited. Give the right source numbers.
- needs_citation: a factual claim with no citation that the sources support.
  Give the source numbers.
- unsupported: a claim no finding supports, or one that contradicts a finding.
  Say what is unsupported in the note.
A statement about the research itself ("no study comparing X was found") is
supported when a finding says so. Prefer the source whose quotes state the
claim. Judge support only, not whether a source is reputable.

FINDINGS (JSON):
{findings}

SOURCES (JSON):
{sources}

{marker}
{sentences}"""

SENTENCES_MARKER = "BRIEFING SENTENCES (JSON):"
VERIFIER_MAX_TOKENS = 32_000

REVISE = """Some sentences in this research briefing make claims the findings
do not support. Rewrite each flagged sentence so it says only what the findings
support, citing sources as [n], or return empty text to delete it. Return one
rewrite per flagged sentence, by index. The whole briefing is there for
context; do not rewrite anything else.

{today}

Flagged sentences (JSON), each with the reviewer's note:
{problems}

Findings:
{findings}

Briefing:
{briefing}"""


VERIFY_ATTEMPTS = 2


async def parsed(model: Any, prompt: list[Any], role: str) -> BaseModel | None:
    """Structured output, or None after ``VERIFY_ATTEMPTS`` malformed replies.

    Haiku 5.5 reasons before it answers, and on a long briefing the reasoning
    can use up ``max_tokens`` and leave no JSON. One perovskite eval case did
    so on four attempts in a row, failing the whole run under node retries.
    """
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        try:
            result: BaseModel = await model.ainvoke(prompt)
        except (OutputParserException, ValidationError) as exc:
            log.warning(
                "%s output unparseable, attempt %d of %d: %s",
                role,
                attempt,
                VERIFY_ATTEMPTS,
                str(exc)[:200],
            )
        else:
            return result
    return None


def verify_sources(findings: list[Finding], numbers: dict[str, int]) -> list[dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for i, f in enumerate(findings):
        for s in f["sources"]:
            n = numbers[s["url"]]
            if n in out:
                continue
            entry: dict[str, Any] = {"n": n, "title": s["title"], "finding": i}
            if quotes := s.get("quotes"):
                entry["quotes"] = quotes
            if f.get("cited") is False:
                entry["retrieved_only"] = True
            out[n] = entry
    return [out[n] for n in sorted(out)]


@dataclass(slots=True)
class Checked:
    """A verified answer: the text with its source list, and what changed."""

    text: str
    counts: dict[str, Any] = field(default_factory=dict)


async def check_citations(
    report: str,
    findings: Sequence[Finding],
    numbers: dict[str, int],
    *,
    verifier: Any,
    reviser: Any,
    today: str,
    rendered_findings: str,
    reviser_system: str = "You write briefings for busy people.",
) -> Checked | None:
    """Verify ``report`` (body, then an optional ``\n\nSources\n`` listing).

    ``numbers`` maps each source URL in ``findings`` to its ``[n]``.
    ``verifier`` and ``reviser`` are the bound structured models;
    ``rendered_findings`` is how the reviser sees the evidence. Returns None
    when there is nothing to check (no sentences, or no sources).
    """
    body, sep, listing = report.partition("\n\nSources\n")
    paragraphs = [split_sentences(p) for p in body.split("\n")]
    flat = [(pi, si, s) for pi, para in enumerate(paragraphs) for si, s in enumerate(para)]
    if not flat or not numbers:
        return None

    prompt = [
        HumanMessage(
            VERIFY.format(
                today=today,
                findings=json.dumps(
                    [
                        {"finding": i, "question": f["question"], "answer": f["answer"]}
                        for i, f in enumerate(findings)
                    ],
                    indent=1,
                ),
                sources=json.dumps(verify_sources(list(findings), numbers), indent=1),
                marker=SENTENCES_MARKER,
                sentences=json.dumps(
                    [{"index": i, "sentence": s} for i, (_, _, s) in enumerate(flat)],
                    indent=1,
                ),
            )
        )
    ]
    result = await parsed(verifier, prompt, "verifier")
    if result is None:
        # Verification improves an answer; it must never cost the user one.
        return Checked(report, {"sentences": len(flat), "skipped": "unparseable output"})
    assert isinstance(result, Verification)

    valid = set(numbers.values())
    counts: dict[str, Any] = {"sentences": len(flat), "recited": 0, "unsupported": 0, "revised": 0}
    flagged: dict[int, dict[str, Any]] = {}
    for check in result.issues:
        if not 0 <= check.index < len(flat):
            continue
        pi, si, sentence = flat[check.index]
        chosen = [n for n in check.sources if n in valid][:3]
        if check.verdict in {"wrong_source", "needs_citation"} and chosen:
            paragraphs[pi][si] = cite(sentence, chosen)
            counts["recited"] += 1
        elif check.verdict == "unsupported":
            counts["unsupported"] += 1
            flagged[check.index] = {
                "index": check.index,
                "sentence": sentence,
                "note": check.note or "not supported",
            }

    if flagged:
        revision = await parsed(
            reviser,
            [
                SystemMessage(reviser_system),
                HumanMessage(
                    REVISE.format(
                        today=today,
                        problems=json.dumps(list(flagged.values()), indent=1),
                        findings=rendered_findings,
                        briefing="\n".join(" ".join(p) for p in paragraphs),
                    )
                ),
            ],
            "reviser",
        )
        assert revision is None or isinstance(revision, Revision)
        for rewrite in revision.rewrites if revision else []:
            text = rewrite.text.strip()
            # A rewrite citing a number with no source is worse than none.
            if rewrite.index not in flagged or {int(n) for n in CITATION.findall(text)} - valid:
                continue
            pi, si, _ = flat[rewrite.index]
            paragraphs[pi][si] = text
            counts["revised"] += 1

    fixed = "\n".join(" ".join(s for s in p if s) for p in paragraphs)
    return Checked(fixed + (sep + listing if sep else ""), counts)
