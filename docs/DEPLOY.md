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
from this workspace, so the limit is the real cap on cost; the rate limits
below only shape it.

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
| `VOYAGE_API_KEY` | From [dashboard.voyageai.com](https://dashboard.voyageai.com/), with a payment method on the account (without one, Voyage allows 3 requests a minute, too few to index `docs/`). Embeds and reranks the public docs corpus. |

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
docker compose ps                                  # all healthy
curl -s 127.0.0.1:8080/api/v1/readyz               # {"status":"ready",...}
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

Neither key above can create corpora or upload documents except the admin
key (`*`): that needs the `corpora:write` scope. Every key can search the
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
example) but cannot upload: their keys lack `corpora:write`. The Anthropic
workspace's spend limit still caps all of them together.

Who has tried it, and when:

```bash
docker compose exec postgres psql -U waypoint -c \
  "select tenant_id, count(*) runs, max(started_at) last from run_audit where tenant_id like 'rev-%' group by 1 order by 3 desc"
```

## Operating it

| Task | Command |
|---|---|
| Update to `main` | `git pull && docker compose up -d --build` |
| Logs | `docker compose logs -f api`; `journalctl -u caddy -f` |
| Restart one service | `docker compose restart api` |
| Re-index the docs corpus now | `docker compose exec api python -m waypoint.seed` (prints what it indexed, kept and removed) |
| Back up Postgres | `docker compose exec -T postgres pg_dump -U waypoint waypoint \| gzip > ~/backups/waypoint-$(date +%F).sql.gz` |
| Restore | `gunzip -c <file> \| docker compose exec -T postgres psql -U waypoint waypoint` |
| Stop the demo | `docker compose down` (data stays in the volumes) |

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
