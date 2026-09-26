"""Read-only adapters over the real systems: Supabase telemetry, Langfuse, GitHub, PostHog.

Every function returns plain data plus the evidence that backs each number it reports.
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import psycopg
from langfuse import get_client
from psycopg.rows import dict_row

from regress_mcp.store import Evidence
from target import config, config_repo
from target.prompts_registry import production_version

WINDOW_STATS_SQL = """
select {group_expr} as segment,
  count(*) as requests,
  count(*) filter (where golden_id is not null and not provider_error) as golden_requests,
  avg(eval_score) filter (where golden_id is not null and not provider_error) as eval_score,
  avg(format_valid::int) filter (where golden_id is not null and not provider_error) as format_valid,
  avg(escalation_correct::int) filter (where golden_id is not null and not provider_error) as escalation_correct,
  avg(citation_correct::int) filter (where golden_id is not null and not provider_error) as citation_correct,
  avg(provider_error::int) as provider_error_rate,
  percentile_cont(0.5) within group (order by latency_ms) filter (where not provider_error) as latency_p50_ms,
  percentile_cont(0.95) within group (order by latency_ms) filter (where not provider_error) as latency_p95_ms,
  avg(cost_usd) filter (where not provider_error) as cost_per_request_usd
from requests
where ts >= %(from)s and ts < %(to)s and source <> 'probe'
group by 1 order by 1
"""
GROUPABLE = {"prompt_version", "model", "category", "source"}


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def window_stats(conn: psycopg.Connection, start: datetime, end: datetime, group_by: str | None) -> dict:
    if group_by is not None and group_by not in GROUPABLE:
        raise ValueError(f"group_by must be one of {sorted(GROUPABLE)}")
    sql = WINDOW_STATS_SQL.format(group_expr=f"{group_by}::text" if group_by else "'all'")
    params = {"from": start, "to": end}
    rows = conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()
    source = {"kind": "sql", "query": sql, "params": {k: _iso(v) for k, v in params.items()}}
    evidence, segments = [], []
    for row in rows:
        ids = {}
        for metric, value in row.items():
            if metric == "segment" or value is None:
                continue
            ev = Evidence(f"{metric} for {group_by or 'all'}={row['segment']}", float(value),
                          "count" if metric.endswith("requests") else metric.rsplit("_", 1)[-1] if metric.endswith(("_ms", "_usd")) else "ratio",
                          source, "regress-mcp/window_stats", _iso(start), _iso(end))
            evidence.append(ev)
            ids[metric] = ev.id
        # Postgres averages arrive as Decimal; everything downstream does float arithmetic.
        segments.append({**{k: (float(v) if isinstance(v, (int, float, Decimal)) and k != "segment" else v)
                            for k, v in row.items()}, "evidence": ids})
    return {"window": {"from": _iso(start), "to": _iso(end)}, "group_by": group_by,
            "segments": segments, "evidence": evidence}


def changes(conn: psycopg.Connection, start: datetime, end: datetime) -> dict:
    """Config changes in the window from change_log, joined with their commits in the config repo."""
    sql = """select id, ts, kind, target, from_value, to_value, actor, commit_sha, note
             from change_log where ts >= %(from)s and ts < %(to)s order by ts"""
    params = {"from": start, "to": end}
    rows = conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()
    commits = _config_commits(start - timedelta(minutes=5), end) if config_repo.enabled() else {}
    source = {"kind": "sql", "query": sql, "params": {k: _iso(v) for k, v in params.items()}}
    result, evidence = [], []
    for row in rows:
        commit = commits.get(row["commit_sha"] or "")
        ev = Evidence(f"{row['kind']} change {row['target']}: {row['from_value']} -> {row['to_value']}",
                      row["ts"].timestamp(), "unix_ts", source, "regress-mcp/changes", _iso(row["ts"]), _iso(row["ts"]))
        evidence.append(ev)
        result.append({
            "change_id": row["id"], "ts": _iso(row["ts"]), "kind": row["kind"], "target": row["target"],
            "from": row["from_value"], "to": row["to_value"], "actor": row["actor"],
            "by_regress": row["actor"].startswith("regress"),
            "commit": commit or ({"sha": row["commit_sha"]} if row["commit_sha"] else None),
            "evidence": ev.id,
        })
    return {"window": {"from": _iso(start), "to": _iso(end)}, "changes": result, "evidence": evidence}


def _config_commits(start: datetime, end: datetime) -> dict[str, dict]:
    repo = config.env("CONFIG_REPO")
    token = config_repo._token()
    resp = httpx.get(
        f"https://api.github.com/repos/{repo}/commits",
        params={"since": _iso(start), "until": _iso(end), "per_page": 100},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=20,
    )
    resp.raise_for_status()
    return {c["sha"]: {"sha": c["sha"], "message": c["commit"]["message"], "url": c["html_url"],
                       "author": c["commit"]["author"]["name"], "ts": c["commit"]["author"]["date"]}
            for c in resp.json()}


def traces(conn: psycopg.Connection, start: datetime, end: datetime, *, prompt_version: int | None = None,
           model: str | None = None, category: str | None = None, golden_only: bool = True,
           limit: int = 50) -> dict:
    """Recent real requests: the inputs a replay re-executes. Newest first, one per golden question."""
    filters, params = ["ts >= %(from)s", "ts < %(to)s", "not provider_error", "source <> 'probe'"], {"from": start, "to": end, "limit": limit}
    if golden_only:
        filters.append("golden_id is not null")
    for name, value in (("prompt_version", prompt_version), ("model", model), ("category", category)):
        if value is not None:
            filters.append(f"{name} = %({name})s")
            params[name] = value
    sql = f"""select distinct on (golden_id) trace_id, ts, golden_id, category, question, prompt_version, model, eval_score
              from requests where {' and '.join(filters)} order by golden_id, ts desc"""
    sql = f"select * from ({sql}) t order by ts desc limit %(limit)s"
    rows = conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()
    return {"window": {"from": _iso(start), "to": _iso(end)},
            "traces": [{**r, "ts": _iso(r["ts"]), "eval_score": r["eval_score"]} for r in rows],
            "source": {"kind": "sql", "query": sql}}


def route(conn: psycopg.Connection) -> dict:
    row = conn.cursor(row_factory=dict_row).execute(
        "select name, model, updated_at, updated_by from routes where name = %s", (config.ROUTE_NAME,)).fetchone()
    return {**row, "updated_at": _iso(row["updated_at"])}


def prompt(version: int | None) -> dict:
    langfuse = get_client()
    name = config.prompt_name()
    fetched = langfuse.api.prompts.get(name, version=version) if version else langfuse.api.prompts.get(name, label="production")
    return {"name": name, "version": fetched.version, "labels": fetched.labels, "config": fetched.config,
            "text": fetched.prompt, "commit_message": getattr(fetched, "commit_message", None),
            "production_version": production_version(langfuse, name)}


def user_signals(minutes: int, end: datetime | None = None) -> dict:
    """Thumbs-down and talk-to-human events from PostHog in the window, vs the previous 2 hours."""
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    project, key = os.environ.get("POSTHOG_PROJECT_ID"), os.environ.get("POSTHOG_PERSONAL_API_KEY")
    if not project or not key:
        return {"available": False, "reason": "POSTHOG_PROJECT_ID / POSTHOG_PERSONAL_API_KEY not set", "evidence": []}
    host = os.environ.get("POSTHOG_APP_HOST", "https://us.posthog.com")
    hogql = (
        "select if(timestamp >= toDateTime({start}), 'current', 'baseline') as part, event, count() "
        "from events where timestamp >= toDateTime({base_start}) and timestamp < toDateTime({end}) "
        "and event in ('thumbs_down', 'thumbs_up', 'talk_to_human') group by part, event"
    )
    values = {"start": start, "base_start": start - timedelta(minutes=120), "end": end}
    query = hogql.format(**{k: f"'{v:%Y-%m-%d %H:%M:%S}'" for k, v in values.items()})
    resp = httpx.post(f"{host}/api/projects/{project}/query/", headers={"Authorization": f"Bearer {key}"},
                      json={"query": {"kind": "HogQLQuery", "query": query}}, timeout=30)
    resp.raise_for_status()
    counts = {(part, event): n for part, event, n in resp.json()["results"]}
    source = {"kind": "posthog_hogql", "query": query}
    evidence, summary = [], {}
    for part in ("current", "baseline"):
        for event in ("thumbs_down", "talk_to_human"):
            n = counts.get((part, event), 0)
            ev = Evidence(f"PostHog {event} events, {part} window", float(n), "count", source,
                          "regress-mcp/user_signals",
                          _iso(start if part == "current" else values["base_start"]),
                          _iso(end if part == "current" else start))
            evidence.append(ev)
            summary[f"{event}_{part}"] = {"count": n, "evidence": ev.id}
    return {"available": True, "window": {"from": _iso(start), "to": _iso(end)}, "baseline_minutes": 120,
            **summary, "evidence": evidence}
