# Deploy

Running Waypoint as a public demo on one Linux host: Docker Compose for the
stack, Caddy in front for TLS. This is how `waypoint.brentdunham.com` runs.

```
internet -> :443 Caddy (TLS, one site per demo)
              -> 127.0.0.1:8080 web container (nginx: SPA, /api proxied, SSE unbuffered)
                   -> api -> postgres, redis    (no host ports)
```

The portfolio site lives on Vercel and links here. Waypoint cannot run on
Vercel: runs are long-lived tasks and event streams stay open for minutes.
Proxying `/api` through a Vercel rewrite would cut each connection at the
CDN's origin timeout.

## Prerequisites

* A DNS A record for the demo's hostname pointing at the host.
* Ubuntu with Docker Engine and the Compose plugin
  ([docs.docker.com/engine/install/ubuntu](https://docs.docker.com/engine/install/ubuntu/)),
  and Caddy 2. (If Caddy's apt repository fails, install the `.deb` from its
  GitHub releases.)
* ufw allowing only SSH, 80 and 443, and the provider's own firewall allowing
  80 and 443. Docker's published ports bypass ufw, which is why
  `compose.prod.yaml` publishes nothing except the web container on loopback.
* SSH with keys only (`PasswordAuthentication no`).

## 1. An Anthropic key with a spend limit

Create a separate workspace in the Claude Console for the demo, set its
monthly spend limit, and create an API key in it. Anyone using the demo spends
from this workspace, so the limit is the backstop on cost. The service's own
daily budgets (`BUDGET__*`, below) are the cap you set: $0.70 a day across
all reviewers, so about $21 in any 30 days, with a clean `429` instead of
failed runs when it is reached. Set the workspace limit a little above that.

## 2. Code and configuration

```bash
sudo mkdir -p /srv && sudo chown "$USER" /srv
git clone https://github.com/brentdunham/waypoint.git /srv/waypoint
cd /srv/waypoint
cp deploy/.env.production.example .env
chmod 600 .env
```

Fill in `.env`:

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD`, `SECRET_KEY` | `openssl rand -hex 32` each |
| `ANTHROPIC_API_KEY` | The demo workspace's key |
| `BOOTSTRAP_API_KEY` | `python3 -c "import secrets; print(f'ak_{secrets.token_hex(6)}.{secrets.token_urlsafe(32)}')"`; first boot only |
| `WEB_PORT` | `8080` for this demo; a different loopback port for each other demo on the host |
| `NOTES_TOKEN` | `openssl rand -hex 24`. The shared secret of the demo notes server; put the same value in `MCP__SERVERS` (next row) |
| `MCP__SERVERS` | `'[{"name": "notes", "url": "http://notes:8001/mcp", "trusted": true, "token": "<NOTES_TOKEN>"}]'` (single quotes: JSON). Gives Workspace the notes server's tools; see [Notes server](#notes-server) |
| `VOYAGE_API_KEY` | From [dashboard.voyageai.com](https://dashboard.voyageai.com/), with a payment method on the account (without one, Voyage allows 3 requests a minute, too few to index `docs/`). Embeds and reranks the public docs corpus. |

The example file sets the budgets to $0.35 per tenant and $0.70 for the server
per day. Exempt your own tenant so your use never spends the reviewers'
allowance; find it after the first boot with

```bash
docker compose exec postgres psql -U waypoint -tc \
  "select distinct tenant_id from api_keys where revoked_at is null and ('*' = any(scopes) or 'keys:admin' = any(scopes))"
```

and set `BUDGET__TENANTS='{"<that tenant>": null}'` (single quotes: the value
is JSON), then `docker compose up -d` to apply it.

The example file already sets `RAG__EMBEDDINGS_MODEL=voyageai:voyage-4` and
`RAG__SEED_DIR=docs`: the API image contains this repository's `docs/`, and
every boot keeps them indexed as the public `waypoint-docs` corpus, which
every key can search. Leave `RAG__EMBEDDINGS_MODEL` empty to run without
documents.

`COMPOSE_FILE` in that file makes every `docker compose` command below use
`compose.yaml` plus `compose.prod.yaml`.

## 3. Start

```bash
docker compose up -d --build
docker compose ps                                  # all healthy, two worker containers
curl -s 127.0.0.1:8080/api/v1/readyz               # {"status":"ready",..."workers":"ok (2)"...}
docker compose logs api | grep "bootstrap API key active"
docker compose logs api | grep "seeded waypoint-docs"     # 18 indexed, ... in ~10s
```

Seeding runs in the background, so the API answers before it finishes. The
first boot embeds all of `docs/` (about 60k tokens, inside Voyage's free
tier); later boots re-embed only the pages a deploy changed and delete the
ones it removed.

## 4. Caddy

Give Caddy a directory of sites, one file per demo:

```bash
sudo mkdir -p /etc/caddy/sites
echo 'import /etc/caddy/sites/*.caddy' | sudo tee /etc/caddy/Caddyfile
sudo cp deploy/caddy/waypoint.caddy /etc/caddy/sites/
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl -s https://waypoint.brentdunham.com/api/v1/healthz
```

The first HTTPS request may take a few seconds while Caddy obtains the
certificate. `journalctl -u caddy -f` shows it.

## 5. API keys

The bootstrap key has every scope. Use it once to mint two keys, then retire
it.

```bash
BASE=https://waypoint.brentdunham.com/api/v1
BOOT="Authorization: Bearer <BOOTSTRAP_API_KEY>"

# Your admin key: keep it private.
curl -s -X POST $BASE/keys -H "$BOOT" -H 'Content-Type: application/json' \
  -d '{"tenant_id":"brent","user_id":"brent","scopes":["*"],"label":"admin"}'

# The public demo key: no admin scopes.
curl -s -X POST $BASE/keys -H "$BOOT" -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","user_id":"visitor","scopes":["agents:run","threads:read","threads:write"],"label":"public demo"}'
```

Each response contains `api_key`, shown once. Then revoke the bootstrap key
with the admin key, remove it from `.env`, and restart the API:

```bash
ADMIN="Authorization: Bearer <admin api_key>"
curl -s -X DELETE $BASE/keys/<bootstrap key_id> -H "$ADMIN"   # key_id: the part between ak_ and the dot
sed -i 's/^BOOTSTRAP_API_KEY=.*/BOOTSTRAP_API_KEY=/' .env
docker compose up -d api
```

Of the keys above only the admin key (`*`) can create corpora or upload
documents: that needs the `corpora:write` scope. Every key can search the
public `waypoint-docs` corpus.

Visitors paste the demo key into the connection bar. All of them share the
`demo` tenant: its rate limits (`LIMITS__RPM`, `LIMITS__CONCURRENT_RUNS`) are
for everyone together, and threads are shared because the user is the same.
Rotate the demo key by minting a new one and revoking the old.

## Reviewer links

Rather than one shared key, send each reviewer a personal link:

```bash
export WAYPOINT_ADMIN_KEY=<admin api_key>
deploy/reviewer-link.sh "Anthropic" "Jane Doe, AI platform"
# tenant  rev-anthropic
# key_id  3f9a...
# link    https://waypoint.brentdunham.com/#key=ak_3f9a....
```

Opening the link stores the key in the browser and removes it from the
address bar. The key travels in the URL fragment, which browsers never send to
the server, so it is not in access logs. Each reviewer is their own tenant:
their threads are private, they get their own rate limits, and revoking one
key (the script prints the command) affects no one else. They can run research
over the public `waypoint-docs` corpus (the welcome panel has a one-click
example), and in Workspace create corpora of their own and upload into them
(`corpora:write`), within the daily upload limits below. The Anthropic
workspace's spend limit still caps all of them together.

**Upload limits** (`UPLOADS__*`, CONFIGURATION.md): each reviewer tenant may
own 3 corpora and upload 10 files, 5 MB and 2,000 chunks per UTC day; the
dollar budget applies on top. At about $0.016 of embedding per MB (estimate
in docs/design/tools.md, phase 2b), a reviewer's full day of uploads is under
$0.10. Admin keys are not limited.

Keys minted before 2026-10-14 lack `corpora:write`. To let them upload too:

```bash
docker compose exec postgres psql -U waypoint -c \
  "update api_keys set scopes = array_append(scopes, 'corpora:write') where tenant_id like 'rev-%' and not ('corpora:write' = any(scopes))"
```

The API caches a key for 30 seconds, so the change applies within that.

Who has tried it, and when:

```bash
docker compose exec postgres psql -U waypoint -c \
  "select tenant_id, count(*) runs, max(started_at) last from run_audit where tenant_id like 'rev-%' group by 1 order by 3 desc"
```

What the demo has cost, per tenant, over the last 30 days (runs from before
metering have no cost and count as zero):

```bash
docker compose exec postgres psql -U waypoint -c \
  "select tenant_id, count(*) runs, round(sum(cost_usd), 2) usd from run_audit where started_at > now() - interval '30 days' group by 1 order by 3 desc nulls last"
```

## Global corpora

Corpora every reviewer can select in Workspace, besides the seeded
`waypoint-docs`. With an admin key (`*` or `keys:admin`): create a corpus,
upload into it while it is private, then publish it.

```bash
ADMIN="Authorization: Bearer $BOOTSTRAP_API_KEY"
BASE=https://waypoint.brentdunham.com/api/v1
ID=$(curl -s -X POST $BASE/corpora -H "$ADMIN" -H 'Content-Type: application/json' -d '{"name":"handbook"}' | jq -r .corpus_id)
curl -s -X POST $BASE/corpora/$ID/documents -H "$ADMIN" -F "files=@handbook.md"
# wait for the ingest run to finish (GET /corpora/$ID/documents shows each status), then:
curl -s -X POST $BASE/corpora/$ID/visibility -H "$ADMIN" -H 'Content-Type: application/json' -d '{"visibility":"public"}'
```

To change one later, set it back to `private`, upload or delete, and publish
again; while private it disappears from other tenants' pickers.

## Workers

Runs execute in the `worker` service, not in `api` (ARCHITECTURE §2a,
[design/queue.md](design/queue.md)). The api accepts a run and puts it on a
Redis queue; a worker takes it off and drives it. Same image as the api,
command `python -m waypoint.worker`, no port, `WORKER_REPLICAS` (2)
containers of `QUEUE__CONCURRENCY` (8) runs each, both set in `.env`.

```bash
docker compose ps worker                                  # two, healthy
curl -s 127.0.0.1:8080/api/v1/readyz | jq .checks         # "workers": "ok (2)", "queue": "0 queued, 0 running"
docker compose logs -f worker                             # run logs live here now
docker stats --no-stream $(docker compose ps -q worker)   # about 140 MB each at rest
```

`workers: unavailable` means no worker has reported in the last 30 seconds.
The api keeps accepting runs, which wait in the queue (`queue: 3 queued`)
until a worker starts. `docker compose logs worker` says why it is not up.

A worker that is stopped (a deploy, `docker compose restart worker`) takes no
new runs and gives those in flight 20 seconds (`QUEUE__DRAIN_S`) to end; a
run still going after that is handed off without a terminal event. A dead
worker's runs are reclaimed after the owner lease expires (30 s by default),
on the next scan (10 s). Restarting or deploying only the api touches no
runs. Recovery requires Postgres checkpoints and Redis persistence.

For recovery acceptance after a deployment: start
and approve a research run, find its driving container in worker logs,
kill that container, and check that the same event stream has one
`run.reclaimed` and one terminal event. Repeat with a worker restart during
a run longer than the drain. Check for doubled partial text and usage from
both attempts. Repeat an approved notes write across a handoff and check
that only one note exists. These checks passed on Bob on 2026-10-15, including
a browser-driven graceful handoff and notes replay after worker restart.
See [NEXT-STEPS.md](NEXT-STEPS.md) for run ids, measured cost and validation.

## Notes server

The demo MCP server (`waypoint.mcp_demo.notes`, the `notes` compose service):
a notes store kept apart per tenant, with `search` and `list` (read-only, run
at once) and `add` and `delete` (Workspace stops for the person's approval
first). Same image as the api, its own command. It is on the compose network
only, publishes no port, and refuses every request except `/healthz` without
`NOTES_TOKEN`. Notes live in SQLite on the `notesdata` volume.

With `NOTES_TOKEN` and `MCP__SERVERS` set as above:

```bash
docker compose ps notes                                   # healthy
curl -s 127.0.0.1:8080/api/v1/readyz | jq .checks         # "mcp:notes": "ok"
docker compose logs worker | grep "MCP server 'notes'"    # 4 tools (notes_search, ...)
```

`mcp:notes: unavailable` means the workers could not list the tools: a wrong
token (the server answers 401) or the service down. They keep running and
retry every `MCP__REFRESH_S` (5 minutes); `docker compose restart worker`
tries at once.

A key needs `tools:notes`. Reviewer links minted from now on have it. For
keys minted before:

```bash
docker compose exec postgres psql -U waypoint -c \
  "update api_keys set scopes = array_append(scopes, 'tools:notes') where tenant_id like 'rev-%' and not ('tools:notes' = any(scopes))"
```

Your own keys with `*` already have every scope. Each tenant sees only its
own notes: Waypoint sends the run's tenant with every call, and the server
files and reads notes under it.

## Operating it

| Task | Command |
|---|---|
| Update to `main` | `git pull && docker compose up -d --build` |
| Logs | `docker compose logs -f api` (requests), `docker compose logs -f worker` (runs); `journalctl -u caddy -f` |
| Restart one service | `docker compose restart api` (runs are not touched) or `docker compose restart worker` (runs in flight get 20 s to end) |
| Re-index the docs corpus now | `docker compose exec api python -m waypoint.seed` (prints what it indexed, kept and removed) |
| Back up Postgres | `docker compose exec -T postgres pg_dump -U waypoint waypoint \| gzip > ~/backups/waypoint-$(date +%F).sql.gz` |
| Restore | `gunzip -c <file> \| docker compose exec -T postgres psql -U waypoint waypoint` |
| Stop the demo | `docker compose down` (data stays in the volumes) |
| Open Jaeger (all traces, 7 days) | on your machine: `ssh -L 16686:127.0.0.1:16686 brent@bob`, then http://localhost:16686 |
| Find a run's trace | `trace_id` is on the run record and in `run_audit`; paste it into Jaeger's search |

Traces go to the `jaeger` service in `compose.prod.yaml` (Jaeger v2, Badger
storage on the `jaeger-data` volume, kept 7 days by `deploy/jaeger/config.yaml`).
Its UI shows every tenant's runs, so it is published on Bob's loopback only and
never through Caddy; reach it over SSH. Reviewers see their own runs' timelines
in the app instead (`TRACING__PANEL=true`). Turning the timeline off is
`TRACING__PANEL=false` and `docker compose up -d`; tracing itself keeps going.

A nightly backup, kept for 14 days:

```bash
mkdir -p ~/backups
( crontab -l 2>/dev/null; echo '15 4 * * * cd /srv/waypoint && docker compose exec -T postgres pg_dump -U waypoint waypoint | gzip > ~/backups/waypoint-$(date +\%F).sql.gz && find ~/backups -name "waypoint-*.sql.gz" -mtime +14 -delete' ) | crontab -
```

## Adding another demo on the same host

1. A DNS A record for its hostname.
2. Its compose stack with only its web entry point published on a new
   loopback port (`127.0.0.1:8081`), and a unique compose project `name`.
3. A file in `/etc/caddy/sites/` with its hostname and that port, then
   `sudo systemctl reload caddy`.
4. If it runs its own Jaeger, a different loopback port than 16686 for its UI.
