# Working notes for Claude sessions

Read this first. It is what a fresh session needs to work in this repo without
rediscovering it.

## Where things are

* `README.md`, `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md`,
  `docs/TESTING.md`, `docs/DEPLOY.md`: the reference docs.
* `docs/guide/`: the long-form design guide for reviewers (Mermaid diagrams,
  glossary). Keep it in step with any architecture change.
* `docs/ROADMAP.md`: what is done and what is next.
* `docs/design/`: design docs. **Current: observability and cost,
  `docs/design/observability.md`; complete (all four phases, deployed 2026-10-13); its case study is `docs/case-studies/cost.md`.** Next per ROADMAP: MCP tools and tool-level approval (item 5), with prompt caching as a smaller open item. `rag.md` is
  complete (all five phases, deployed 2026-10-11) and is the model for format.
  Build it phase by phase as written; record any deviation in the doc and in
  ROADMAP.md.
* `docs/case-studies/`: write-ups of measured changes (`grounded.md` is the
  model to follow; `retrieval.md` covers RAG phase 4).
* `src/waypoint/`: the service. `agents/graphs/research.py` is the research
  agent; `agents/models.py` builds models and embeddings; `infra/db.py` owns
  the schema and `migrate()`.
* `evals/`: datasets, rubrics, calibration, baselines, results.
* `web/`: React/Vite UI.

## Environment

The dev machine is Windows (`C:\projects\waypoint`), PowerShell.

* Run tools as modules: `uv run python -m pytest`, `uv run python -m mypy`,
  `uv run ruff check src tests evals`, `uv run ruff format --check src tests evals`,
  `uv run python -m evals ...`. The `uv run <script>`
  entry points can fail on Windows with the uv trampoline.
* Web: `cd web; npm test; npm run build`.
* psycopg needs the selector event loop on Windows; `src/waypoint/loops.py` provides it.
  Use that in any new entry point.
* PowerShell tool calls time out after about 60 seconds. Start long jobs
  (evals, builds) with `Start-Process` and redirect output to a log file, then
  poll the log.
* Files use LF line endings. Commit with `git -c core.autocrlf=false`. When
  writing files from PowerShell, use `[IO.File]::WriteAllText` (UTF-8, no BOM),
  not `Set-Content`.
* Tools cannot write under `.github/`. Give Brent the change to make there.
* `docker --env-file` does not strip inline comments; keep env files to
  plain `KEY=value` lines.

## Before every commit

All green: pytest, ruff check and format, mypy, `npm test`, `npm run build`. Tests are
hermetic (`FAKE_MODEL`, `HashEmbeddings`); never make a test need a network
or a key.

## Evals

* Iterate with `--trials 1`; accept a baseline only with `--trials 3`.
* Model calls are recorded and replayed. The replay store lives beside
  `--out`, so a new `--out` directory means an empty store and every call runs
  live and costs money. Reuse the existing results directory unless a fresh
  sample is the point (`--fresh`).
* Every setting that changes behaviour goes into the config fingerprint.
* Live runs cost real money; say what a run will cost before starting a
  3-trial or `--fresh` run.

## Writing style for docs and prose

Plain, direct sentences. No em dashes. Numbers with their source (a scorecard
or a command). Explain why, not only what.

## Commits

Small, one concern each, imperative subject. End messages with the attribution
lines the session provides (Co-Authored-By and Claude-Session). Commit and push
only when Brent asks. Write the message to a file with
`[IO.File]::WriteAllText` and use `git commit -F`.

## Deploy

Production runs on the VPS "bob" behind Caddy at
https://waypoint.brentdunham.com. Deploy is
`cd /srv/waypoint && git pull && docker compose up -d --build` (the env file
sets `COMPOSE_FILE=compose.yaml:compose.prod.yaml`). Reviewer keys come from
`deploy/reviewer-link.sh`. See `docs/DEPLOY.md`.
