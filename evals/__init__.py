"""Evals: does each agent do its job, and with which models.

The hermetic suite in ``tests/`` proves the plumbing. This package runs the
real graphs against a small dataset per agent and scores what comes out:

* ``cases`` -- the dataset format, one JSONL file per agent under ``datasets/``;
* ``driver`` -- runs one case in process, answers interrupts, records every
  model call with its role, model, tokens and latency;
* ``checks`` -- deterministic, cheap assertions on the transcript;
* ``judge`` -- an LLM grades rubric criteria, one criterion per call;
* ``scorecard`` -- aggregates trials, fingerprints the configuration, and
  gates against a baseline;
* ``compare`` -- two configurations head to head, judged pairwise.

Run ``uv run python -m evals --help``. docs/TESTING.md has the workflow.
"""
