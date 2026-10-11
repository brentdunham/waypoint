"""``GET /usage``: what the caller's tenant has spent.

Read from ``run_audit``, which records each run's cost when it ends, so the
numbers outlive the run's Redis record. Only the caller's own tenant: the
server's total and other tenants' spend are not exposed (observability
design, D4). Budgets and the remaining balance join this in phase 2.
"""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query

from waypoint.api.deps import Reader, Runtime
from waypoint.errors import ServiceUnavailable
from waypoint.schemas import AgentSpend, TenantUsage

router = APIRouter(tags=["usage"])

SPEND_SQL = """
select agent,
       count(*)                                   as runs,
       coalesce(sum(cost_usd), 0)::float8         as cost_usd,
       count(*) filter (where cost_usd is null)   as unpriced_runs,
       coalesce(sum(input_tokens), 0)::bigint     as input_tokens,
       coalesce(sum(output_tokens), 0)::bigint    as output_tokens,
       coalesce(sum(searches), 0)::bigint         as searches
from run_audit
where tenant_id = %s and started_at >= %s and ended_at is not null
group by agent
order by agent
"""


@router.get("/usage")
async def usage(
    runtime: Runtime,
    principal: Reader,
    days: Annotated[int, Query(ge=1, le=31, description="Trailing window, in days.")] = 1,
) -> TenantUsage:
    """The tenant's spend over the last ``days`` days (default: the last 24 hours)."""
    if runtime.db is None:
        raise ServiceUnavailable("Usage needs the database (DATABASE_URL is unset).")
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    rows = await runtime.db.fetch_all(SPEND_SQL, (principal.tenant_id, since))
    return TenantUsage(
        tenant_id=principal.tenant_id,
        days=days,
        since=since.isoformat(timespec="seconds"),
        runs=sum(r["runs"] for r in rows),
        cost_usd=round(sum(r["cost_usd"] for r in rows), 6),
        unpriced_runs=sum(r["unpriced_runs"] for r in rows),
        input_tokens=sum(r["input_tokens"] for r in rows),
        output_tokens=sum(r["output_tokens"] for r in rows),
        searches=sum(r["searches"] for r in rows),
        by_agent={
            r["agent"]: AgentSpend(runs=r["runs"], cost_usd=round(r["cost_usd"], 6)) for r in rows
        },
    )
