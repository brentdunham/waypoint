#!/usr/bin/env bash
# Mint a personal demo key for one reviewer and print the link to send them.
#
#   WAYPOINT_ADMIN_KEY=ak_... deploy/reviewer-link.sh "Anthropic" ["Jane Doe, AI platform"]
#
# Each reviewer gets their own tenant (rev-<slug>), so their threads are
# private and one reviewer cannot use up another's rate limit. The key can run
# agents and read and write its own threads; it cannot mint keys. The key is in
# the URL fragment, which browsers never send to the server.
#
# Revoke:  curl -X DELETE "$WAYPOINT_URL/api/v1/keys/<key_id>" -H "Authorization: Bearer $WAYPOINT_ADMIN_KEY"
# Usage:   docker compose exec postgres psql -U waypoint -c \
#            "select tenant_id, count(*) runs, max(started_at) last from run_audit where tenant_id like 'rev-%' group by 1 order by 3 desc"
set -euo pipefail

who="${1:?usage: $0 <company or person> [label]}"
label="${2:-$who}"
base="${WAYPOINT_URL:-https://waypoint.brentdunham.com}"
admin="${WAYPOINT_ADMIN_KEY:?set WAYPOINT_ADMIN_KEY to your admin key}"

slug=$(printf '%s' "$who" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-*//; s/-*$//' | cut -c1-40)
[ -n "$slug" ] || { echo "the name must contain letters or digits" >&2; exit 1; }

body=$(python3 -c 'import json,sys; print(json.dumps({"tenant_id": "rev-" + sys.argv[1], "user_id": "reviewer", "scopes": ["agents:run", "threads:read", "threads:write"], "label": "reviewer: " + sys.argv[2]}))' "$slug" "$label")

response=$(curl -fsS -X POST "$base/api/v1/keys" \
  -H "Authorization: Bearer $admin" -H 'Content-Type: application/json' -d "$body")

key=$(printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
key_id=${key#ak_}; key_id=${key_id%%.*}

printf 'tenant  rev-%s\nkey_id  %s\nlink    %s/#key=%s\n' "$slug" "$key_id" "$base" "$key"
