# Waypoint design guide

This guide explains how Waypoint is built and why. It is the long form of
[ARCHITECTURE.md](../ARCHITECTURE.md), which lists each decision in a
paragraph. Here each part of the system gets diagrams, the reasoning behind
it, the alternatives that were rejected, and definitions for the terms it
introduces.

## Reading paths

**Ten minutes.** [1. Overview](01-overview.md), then the
[decision log](07-decisions.md). Together they show what the system does, the
constraints it was built under, and the trade-offs that shaped it.

**Agents and evaluation.** [4. Agents](04-agents.md) and
[5. Evaluation](05-evaluation.md). These cover the LangGraph agents, how
models are assigned to roles, and the harness that measures the agents:
rubrics, an LLM judge calibrated against human labels, a baseline gate, and
record/replay. The [grounded case study](../case-studies/grounded.md) shows
that harness used on a real problem, and the
[retrieval case study](../case-studies/retrieval.md) shows a separate
retrieval eval deciding which search techniques to keep.

**Running it in production.** [2. Runs](02-runs.md),
[3. State and persistence](03-state-and-persistence.md) and
[6. Security and operations](06-security-and-operations.md).

## Contents

| | Chapter | Covers |
|---|---|---|
| 1 | [Overview](01-overview.md) | Goals, non-goals, system context, components, quality attributes and where each comes from |
| 2 | [Runs](02-runs.md) | A run's lifecycle, the event stream, reconnection, cancellation, limits |
| 3 | [State and persistence](03-state-and-persistence.md) | Threads, checkpoints, interrupts, time travel, context vs state, memory, the node cache |
| 4 | [Agents](04-agents.md) | The registry, the chat and research graphs, model roles, structured output, citations, verification |
| 5 | [Evaluation](05-evaluation.md) | Evals, datasets, checks, rubrics, the judge, calibration, scorecards, the gate, replay, cost |
| 6 | [Security and operations](06-security-and-operations.md) | API keys and scopes, tenant isolation, rate limits, errors, observability, boot and deploy |
| 7 | [Decision log](07-decisions.md) | Each significant decision: context, choice, alternatives, consequences |
| | [Glossary](glossary.md) | Every term used in this guide, defined in one place |

## Conventions

* Diagrams are [Mermaid](https://mermaid.js.org/) and render on GitHub.
* A path in `code font` is relative to the repository root. Each section
  names the files that implement it, so a claim can be checked against code.
* Numbers come from code constants, settings defaults, or eval scorecards, and
  say which. Measured numbers carry the date they were measured.
* "Model" means an LLM. Model names are the ones configured at the time of
  writing (October 2026) and are settings, not code.
