"""The `research` agent: plan, approve, investigate with web search, critique, synthesize.

    plan -> approve (interrupt) -> investigate x N (Send) -> critique (defer)
                                        ^                        |
                                        +---- follow-ups --------+--> synthesize

* ``interrupt()`` -- the graph stops mid-node, checkpoints, and waits for a
  human. Resuming is a *second HTTP request* days later if you like.
* ``Send`` -- map-reduce fan-out; one question per worker, all concurrent.
* ``defer=True`` -- the critic runs once per wave, after every worker lands.
* A bounded loop -- the critic scores each finding and may send a few
  follow-up questions back out, at most ``research.max_rounds`` waves.
* Structured output -- planner and critic return Pydantic objects. Nothing in
  this graph parses model prose.
* Grounding -- workers answer with Anthropic's server-side web search, and a
  finding's sources come from the API's citation metadata, not from the text.
"""

import datetime as dt
import json
import logging
from collections.abc import Callable
from typing import Any

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.constants import TAG_NOSTREAM
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime, get_runtime
from langgraph.types import CachePolicy, RetryPolicy, Send, interrupt
from pydantic import BaseModel, Field, ValidationError

from waypoint.agents.citations import answer_and_sources, number_sources, retrieved_sources
from waypoint.agents.context import AgentContext
from waypoint.agents.models import FINDINGS_MARKER, ModelRouter, RoleModel, structured
from waypoint.agents.state import Finding, ResearchState, WorkerInput
from waypoint.agents.types import Graph
from waypoint.settings import Settings

log = logging.getLogger(__name__)

ROLES = ("planner", "searcher", "critic", "writer")
"""Each model call site. ``MODELS__RESEARCH__<ROLE>`` picks its model."""

MAX_QUESTIONS = 5
UNCITED_CAP = 0.3
"""Highest confidence an uncited finding can get, whatever the critic says:
one with no sources, or with only retrieved ones (``Finding.cited``)."""
RETRIEVED_SOURCES = 5
"""How many search results stand in as sources for an answer that cited none."""

WEB_SEARCH_TOOL = "web_search_20250305"
"""The basic version. Later versions route searches through code execution by
default (``allowed_callers``), which needs a second server tool."""


# -- structured outputs ------------------------------------------------------


class PlannedQuestion(BaseModel):
    question: str = Field(description="One sharp, self-contained research question.")
    why: str = Field(description="One short sentence: what answering it adds to the briefing.")


class ResearchPlan(BaseModel):
    questions: list[PlannedQuestion] = Field(
        description=f"At most {MAX_QUESTIONS} independent questions that together cover the topic."
    )


class Assessment(BaseModel):
    question: str = Field(description="The finding's question, copied exactly.")
    confidence: float = Field(
        description=(
            "0 to 1. How well the answer is supported by its sources. Unsourced, "
            "hedged or internally inconsistent answers score low."
        )
    )
    issue: str = Field(
        description="What is weak, missing or contradicted. Empty string if nothing."
    )


class Critique(BaseModel):
    assessments: list[Assessment] = Field(description="One per finding.")
    follow_ups: list[str] = Field(
        description=(
            "New questions whose answers would close a real gap or settle a "
            "contradiction. Empty if the findings are sufficient. Never repeat a "
            "question already asked."
        )
    )


# -- prompts -----------------------------------------------------------------

TODAY = """Today is {today}. Sources, laws and events may postdate your training
data. A recent or unfamiliar date is not a reason to doubt a source or to call
it speculative; judge a source by who published it and what it says."""
"""Every model call in this graph gets the date. Without it, the first eval
baseline (2026-10-07) found briefings calling their own 2026 search results
possibly fabricated, and the critic marking them down as "future-dated"."""

PLANNER = """Plan research on the topic below. Break it into at most {n} sharp,
independent questions that together cover what a busy reader needs to know.

{today}

Topic: {topic}"""

# Prefix "Answer this research question" is what ScriptedChatModel keys on.
WORKER = """Answer this research question in at most five sentences.
{search_rule}
Be concrete: names, numbers, dates. If sources disagree or evidence is thin,
say so plainly instead of smoothing it over.

{today}

Topic: {topic}
Question: {question}"""

SEARCH_RULE = """Search the web first and base every claim on what you find.
Do not write links, URLs, footnotes or citation markers such as [1]: the
sources you draw on are attached to your answer automatically."""
"""The second sentence is for claude-haiku-5-5, which on 2026-10-08 typed
citations into its prose as markdown links in 6 of 21 runs instead of using
the API's citation blocks, leaving those findings with no sources."""
NO_SEARCH_RULE = "Answer from what you know; you have no way to check sources."

CRITIC_SYSTEM = "You review research findings before they go into a briefing. Be strict."

CRITIC = """{today}

Topic: {topic}
This is investigation round {round} of at most {max_rounds}.

Score every finding. Then list at most {max_follow_ups} follow-up questions,
only for gaps that would change the briefing's conclusion or for claims that
contradict each other. Each follow-up is one short question that a single web
search could answer, not a compound of several. A finding marked "cited": false
searched but tied no claim to a source; its sources are what the search
returned, not evidence for any specific claim. {last_round}

{marker}
{findings}"""

LAST_ROUND = "This is the last round: return no follow-ups."

# Prefix "Write a short briefing" is what ScriptedChatModel keys on.
SYNTH = """Write a short briefing on the topic from the findings below.
Lead with the answer. No filler. Plain text, no markdown. Cite sources inline
as [n], using the number of the source that supports that specific claim; the
numbers are listed with each finding. Cite individual sources, e.g. [2][5],
never a range like [1-7]. Do not add a source list; it is appended
for you. Where a finding has low confidence or a noted issue, say so rather
than stating it as fact.

{today}

Topic: {topic}

Findings:
{findings}"""


def retry_malformed_output(exc: Exception) -> bool:
    """LangGraph's default retry predicate, plus structured output that failed
    to parse or validate.

    The default never retries a ``ValueError``, and both pydantic's
    ``ValidationError`` and langchain's ``OutputParserException`` are one. The
    live evals caught a planner failing a whole run on one malformed reply.
    ``STRUCTURED_OUTPUT=json_schema`` makes this rare; this makes it survivable.
    """
    return isinstance(exc, ValidationError | OutputParserException) or bool(_DEFAULT_RETRY_ON(exc))


# RetryPolicy's own default predicate, read off the type rather than imported
# from LangGraph's private module.
_DEFAULT_RETRY_ON: Callable[[Exception], bool] = RetryPolicy().retry_on  # type: ignore[assignment]


STRUCTURED_RETRY = RetryPolicy(max_attempts=3, retry_on=retry_malformed_output)


class ResearchConfigError(RuntimeError):
    """The provider refused the research request for configuration reasons.

    A RuntimeError so LangGraph's default retry policy does not retry it.
    """


def build(settings: Settings, **compile_kwargs: Any) -> Graph:
    cfg = settings.research
    router = ModelRouter(settings, "research", ROLES)
    # Planner, workers and critic are intermediate steps, not replies. Clients
    # render the token stream as the assistant's answer, so only the
    # synthesizer streams. Progress still shows as per-node `node` events.
    planner = router.bind(
        "planner",
        lambda m: structured(m, ResearchPlan, settings).with_config(tags=[TAG_NOSTREAM]),
    )
    critic = router.bind(
        "critic",
        lambda m: structured(m, Critique, settings).with_config(tags=[TAG_NOSTREAM]),
    )
    writer = router.bind("writer")

    def _searcher(model: Any) -> Any:
        if cfg.web_search:
            model = model.bind_tools(
                [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": cfg.max_searches}]
            )
        return model.with_config(tags=[TAG_NOSTREAM])

    # Not streamed at all: a streamed reply would have its citations
    # reassembled from deltas; the non-streamed one has them whole.
    searcher = router.bind("searcher", _searcher, disable_streaming=True)

    # -- 1. plan --------------------------------------------------------
    async def plan(state: ResearchState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        topic = state.get("topic") or _topic_from_messages(state)
        result = await planner(runtime.context).ainvoke(
            [HumanMessage(PLANNER.format(n=MAX_QUESTIONS, topic=topic, today=_today()))]
        )
        assert isinstance(result, ResearchPlan)
        planned = [q for q in result.questions if q.question.strip()][:MAX_QUESTIONS]
        if not planned:
            # A planner that returns nothing usable must not sink the run.
            planned = [
                PlannedQuestion(question=f"What is {topic}?", why=""),
                PlannedQuestion(question=f"Why does {topic} matter?", why=""),
                PlannedQuestion(question=f"What are the main objections to {topic}?", why=""),
            ]
        return {
            "topic": topic,
            "plan": [q.question.strip() for q in planned],
            "rationale": [q.why.strip() for q in planned],
        }

    # -- 2. human approval ----------------------------------------------
    def approve(state: ResearchState) -> dict[str, Any]:
        """Stop the world and ask a person.

        ``interrupt`` raises out of the node after checkpointing. The next
        request resumes with ``Command(resume={"approved": true})`` and this
        function is re-entered from the top, with ``interrupt`` returning that
        value instead of raising.
        """
        decision = interrupt(
            {
                "kind": "approve_plan",
                "topic": state.get("topic", ""),
                "plan": state.get("plan", []),
                "rationale": state.get("rationale", []),
                "web_search": cfg.web_search,
                "prompt": "Approve this research plan? You may edit the questions.",
            }
        )
        if isinstance(decision, str):
            decision = {"approved": decision.strip().lower() in {"y", "yes", "true", "approve"}}
        approved = bool(decision.get("approved", False))
        edited = decision.get("plan")
        out: dict[str, Any] = {"approved": approved, "round": 1}
        if approved and isinstance(edited, list) and edited:
            out["plan"] = [str(q) for q in edited][:MAX_QUESTIONS]
        return out

    def dispatch(state: ResearchState) -> list[Send] | str:
        if not state.get("approved"):
            return "declined"
        return _sends(state, state.get("plan", []), round_=1, searcher_model=searcher)

    # -- 3. fan-out worker ------------------------------------------------
    async def investigate(state: WorkerInput) -> dict[str, list[Finding]]:
        prompt = WORKER.format(
            today=_today(),
            topic=state["topic"],
            question=state["question"],
            search_rule=SEARCH_RULE if cfg.web_search else NO_SEARCH_RULE,
        )
        try:
            # By the name in the input, not the context: the node cache keys on
            # input, so the model that answers must be the one in the key.
            reply = await searcher.named(state["model"]).ainvoke([HumanMessage(prompt)])
        except Exception as exc:
            if cfg.web_search and type(exc).__name__ == "BadRequestError":
                raise ResearchConfigError(
                    "The model provider rejected a web-search request. Web search must be "
                    "enabled for your organization in the Claude Console, and the model must "
                    "support it. To run without it, set RESEARCH__WEB_SEARCH=false. "
                    f"Provider said: {exc}"
                ) from exc
            raise
        answer, sources = answer_and_sources(reply)
        cited = bool(sources)
        if not cited and cfg.web_search:
            sources = retrieved_sources(reply, RETRIEVED_SOURCES)
        return {
            "findings": [
                Finding(
                    question=state["question"],
                    answer=answer,
                    sources=sources,
                    cited=cited,
                    confidence=None,
                    issue="",
                    round=state["round"],
                )
            ]
        }

    # -- 4. review: score, and maybe go round again ----------------------
    async def critique(state: ResearchState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        findings = state.get("findings", [])
        round_ = state.get("round", 1)
        last = round_ >= cfg.max_rounds or cfg.max_follow_ups == 0
        payload = json.dumps(
            [
                {
                    "question": f["question"],
                    "answer": f["answer"],
                    "sources": [s["url"] for s in f["sources"]],
                    "cited": f.get("cited", True),
                }
                for f in findings
            ],
            indent=1,
        )
        result = await critic(runtime.context).ainvoke(
            [
                SystemMessage(CRITIC_SYSTEM),
                HumanMessage(
                    CRITIC.format(
                        today=_today(),
                        topic=state.get("topic", ""),
                        round=round_,
                        max_rounds=cfg.max_rounds,
                        max_follow_ups=cfg.max_follow_ups,
                        last_round=LAST_ROUND if last else "",
                        marker=FINDINGS_MARKER,
                        findings=payload,
                    )
                ),
            ]
        )
        assert isinstance(result, Critique)

        by_question = {_norm(a.question): a for a in result.assessments}
        reviewed: list[Finding] = []
        for finding in findings:
            verdict = by_question.get(_norm(finding["question"]))
            confidence = _clamp(verdict.confidence) if verdict else 0.5
            if not finding["sources"] or not finding.get("cited", True):
                confidence = min(confidence, UNCITED_CAP)
            reviewed.append(
                {**finding, "confidence": confidence, "issue": verdict.issue if verdict else ""}
            )

        follow_ups: list[str] = []
        if not last:
            asked = {_norm(f["question"]) for f in findings}
            for question in result.follow_ups:
                key = _norm(question)
                if key and key not in asked:
                    asked.add(key)
                    follow_ups.append(question.strip())
            follow_ups = follow_ups[: cfg.max_follow_ups]

        return {
            "findings": reviewed,
            "follow_ups": follow_ups,
            "round": round_ + 1 if follow_ups else round_,
        }

    def after_critique(state: ResearchState) -> list[Send] | str:
        follow_ups = state.get("follow_ups") or []
        if not follow_ups:
            return "synthesize"
        return _sends(state, follow_ups, round_=state.get("round", 1), searcher_model=searcher)

    # -- 5. write it up ----------------------------------------------------
    async def synthesize(state: ResearchState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        findings = state.get("findings", [])
        numbers = number_sources(findings)
        rendered = "\n\n".join(_render_finding(f, numbers) for f in findings)
        reply = await writer(runtime.context).ainvoke(
            [
                SystemMessage("You write briefings for busy people."),
                HumanMessage(
                    SYNTH.format(today=_today(), topic=state.get("topic", ""), findings=rendered)
                ),
            ]
        )
        briefing, _ = answer_and_sources(reply)
        report = briefing + _source_list(findings, numbers)
        return {
            "report": report,
            "messages": [AIMessage(report)],
            "metadata": {
                "finding_count": len(findings),
                "source_count": len(numbers),
                "rounds": state.get("round", 1),
                "web_search": cfg.web_search,
                "agent": runtime.context.agent,
                "models": {role: router.name(role, runtime.context) for role in ROLES},
            },
        }

    def declined(state: ResearchState) -> dict[str, Any]:
        message = "Research plan was declined; nothing was run."
        return {"report": message, "messages": [AIMessage(message)]}

    builder = StateGraph(ResearchState, context_schema=AgentContext)
    builder.add_node("plan", plan, retry_policy=STRUCTURED_RETRY)
    builder.add_node("approve", approve)
    builder.add_node(
        "investigate",
        investigate,
        input_schema=WorkerInput,
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.5),
        # Identical question + topic + round from the same tenant and searcher
        # model within the TTL reuses the previous answer. tenant_id and model
        # are in WorkerInput so they are in the key.
        cache_policy=CachePolicy(ttl=settings.node_cache_ttl_s or None),
    )
    # defer -> runs once per wave, after every fanned-out worker has finished.
    builder.add_node("critique", critique, defer=True, retry_policy=STRUCTURED_RETRY)
    builder.add_node("synthesize", synthesize)
    builder.add_node("declined", declined)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "approve")
    builder.add_conditional_edges("approve", dispatch, ["investigate", "declined"])
    builder.add_edge("investigate", "critique")
    builder.add_conditional_edges("critique", after_critique, ["investigate", "synthesize"])
    builder.add_edge("synthesize", END)
    builder.add_edge("declined", END)

    return builder.compile(name="research", **compile_kwargs)


def _sends(
    state: ResearchState, questions: list[str], *, round_: int, searcher_model: RoleModel
) -> list[Send]:
    topic = state.get("topic", "")
    context = get_runtime(AgentContext).context
    model = searcher_model.name(context)
    return [
        Send(
            "investigate",
            WorkerInput(
                question=q, topic=topic, tenant_id=context.tenant_id, model=model, round=round_
            ),
        )
        for q in questions
    ]


def _render_finding(finding: Finding, numbers: dict[str, int]) -> str:
    # Number *and* title: with numbers alone the writer cited the right finding
    # but the wrong source within it ("the Medium post [5]" when it was [4]).
    cites = "; ".join(f"[{numbers[s['url']]}] {s['title']}" for s in finding["sources"])
    confidence = finding["confidence"]
    lines = [
        f"Q: {finding['question']}",
        f"A: {finding['answer']}",
        (
            f"Sources: {cites or 'none'}"
            if finding.get("cited", True)
            else f"Sources (searched, not cited for any claim): {cites or 'none'}"
        ),
        f"Confidence: {confidence:.1f}" if confidence is not None else "Confidence: unreviewed",
    ]
    if finding["issue"]:
        lines.append(f"Reviewer note: {finding['issue']}")
    return "\n".join(lines)


def _source_list(findings: list[Finding], numbers: dict[str, int]) -> str:
    if not numbers:
        return ""
    titles = {s["url"]: s["title"] for f in findings for s in f["sources"]}
    lines = [f"[{n}] {titles[url]} - {url}" for url, n in numbers.items()]
    return "\n\nSources\n" + "\n".join(lines)


def _today() -> str:
    return TODAY.format(today=dt.date.today().isoformat())


def _norm(question: str) -> str:
    return " ".join(question.lower().split()).rstrip("?")


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _topic_from_messages(state: ResearchState) -> str:
    messages = state.get("messages", [])
    return str(messages[-1].content) if messages else "an unspecified topic"
