"""Regression detector: robust z-score of each signal in the current window vs the previous 2 hours.

All arithmetic runs in one Postgres query over `requests`, so every figure carries the query and
parameters that produced it. Python only packages the rows as evidence.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from regress_mcp.store import Evidence

BASELINE_MINUTES = 120
Z_THRESHOLD = 3.5
MIN_VOLUME = 20
MIN_BASELINE_BUCKETS = 3
DIMENSIONS = {"prompt_version", "model", "category"}


@dataclass(frozen=True)
class Signal:
    name: str
    kind: str          # rate: absolute effect in [0,1]; ratio: multiplicative effect
    bad_direction: int  # -1 lower is worse, +1 higher is worse, 0 either way
    min_effect: float  # rate: >= this absolute change; ratio: >= this factor
    golden_only: bool
    unit: str


SIGNALS = [
    Signal("eval_score", "rate", -1, 0.10, True, "score"),
    Signal("format_valid", "rate", -1, 0.10, True, "ratio"),
    Signal("escalation_correct", "rate", -1, 0.10, True, "ratio"),
    Signal("citation_correct", "rate", -1, 0.10, True, "ratio"),
    Signal("refusal_rate", "rate", 0, 0.10, True, "ratio"),
    Signal("provider_error_rate", "rate", 1, 0.10, False, "ratio"),
    Signal("latency_p50_ms", "ratio", 1, 2.0, False, "ms"),
    Signal("latency_p95_ms", "ratio", 1, 2.0, False, "ms"),
    Signal("cost_per_request_usd", "ratio", 1, 2.0, False, "usd"),
]
SIGNAL_BY_NAME = {s.name: s for s in SIGNALS}

_CFG_ROWS = ", ".join(
    f"('{s.name}', '{s.kind}', {s.bad_direction}, {s.min_effect}, {str(s.golden_only).lower()})" for s in SIGNALS
)

# Buckets are aligned to as_of with the window width, so the current window is exactly one bucket.
DETECTOR_SQL = f"""
with params as (
  select %(as_of)s::timestamptz as as_of,
         make_interval(mins => %(window_minutes)s) as w,
         make_interval(mins => %(baseline_minutes)s) as b
),
cfg(signal, kind, bad_direction, min_effect, golden_only) as (values {_CFG_ROWS}),
scoped as (
  select r.*, date_bin(p.w, r.ts, p.as_of - p.w) as bucket,
         (r.ts >= p.as_of - p.w) as is_current
  from requests r, params p
  where r.ts >= p.as_of - p.w - p.b and r.ts < p.as_of
    {{exclude_clause}}
),
per_bucket as (
  select bucket, bool_or(is_current) as is_current,
    count(*) as n,
    count(*) filter (where golden_id is not null and not provider_error) as n_golden,
    avg(eval_score) filter (where golden_id is not null and not provider_error) as eval_score,
    avg(format_valid::int) filter (where golden_id is not null and not provider_error) as format_valid,
    avg(escalation_correct::int) filter (where golden_id is not null and not provider_error) as escalation_correct,
    avg(citation_correct::int) filter (where golden_id is not null and not provider_error) as citation_correct,
    avg(refusal::int) filter (where golden_id is not null and not provider_error) as refusal_rate,
    avg(provider_error::int) as provider_error_rate,
    percentile_cont(0.5) within group (order by latency_ms) filter (where not provider_error) as latency_p50_ms,
    percentile_cont(0.95) within group (order by latency_ms) filter (where not provider_error) as latency_p95_ms,
    avg(cost_usd) filter (where not provider_error) as cost_per_request_usd
  from scoped group by bucket
),
long as (
  select b.bucket, b.is_current, b.n, b.n_golden, s.signal, s.value::float8 as value
  from per_bucket b,
  lateral (values ('eval_score', b.eval_score), ('format_valid', b.format_valid),
                  ('escalation_correct', b.escalation_correct), ('citation_correct', b.citation_correct),
                  ('refusal_rate', b.refusal_rate), ('provider_error_rate', b.provider_error_rate),
                  ('latency_p50_ms', b.latency_p50_ms), ('latency_p95_ms', b.latency_p95_ms),
                  ('cost_per_request_usd', b.cost_per_request_usd)) as s(signal, value)
  where s.value is not null
),
baseline as (
  select l.signal, percentile_cont(0.5) within group (order by l.value) as median, count(*) as buckets
  from long l join cfg c using (signal)
  where not l.is_current and (case when c.golden_only then l.n_golden else l.n end) >= 3
  group by l.signal
),
mad as (
  select l.signal, percentile_cont(0.5) within group (order by abs(l.value - b.median)) as mad
  from long l join baseline b using (signal) join cfg c using (signal)
  where not l.is_current and (case when c.golden_only then l.n_golden else l.n end) >= 3
  group by l.signal
),
scored as (
  select c.signal, c.kind, c.bad_direction, c.min_effect,
         cur.value as current, case when c.golden_only then cur.n_golden else cur.n end as volume,
         b.median, m.mad, b.buckets as baseline_buckets,
         greatest(m.mad, case when c.kind = 'rate' then 0.02 else 0.05 * abs(b.median) end) as mad_floored
  from cfg c
  join long cur on cur.signal = c.signal and cur.is_current
  join baseline b on b.signal = c.signal
  join mad m on m.signal = c.signal
)
select *,
  0.6745 * (current - median) / nullif(mad_floored, 0) as z,
  case when kind = 'rate' then abs(current - median) >= min_effect
       else median > 0 and (current / median >= min_effect or current / median <= 1 / min_effect) end as effect_ok,
  volume >= %(min_volume)s as volume_ok,
  baseline_buckets >= %(min_baseline_buckets)s as baseline_ok
from scored
order by signal
"""


def _segment_clause(exclude: dict | None, only: dict | None) -> tuple[str, dict]:
    """SQL for `exclude` (drop one segment, for localisation) and `only` (keep one, for verification).

    `only` applies to the current window alone, so the baseline stays the full pre-incident history.
    """
    clauses, params = [], {}
    for key, seg in (("exclude", exclude), ("only", only)):
        if not seg:
            continue
        if seg["dimension"] not in DIMENSIONS:
            raise ValueError(f"{key} dimension must be one of {sorted(DIMENSIONS)}")
        params[f"{key}_value"] = str(seg["value"])
        if key == "exclude":
            clauses.append(f"and r.{seg['dimension']}::text is distinct from %(exclude_value)s")
        else:
            clauses.append(f"and (r.ts < p.as_of - p.w or r.{seg['dimension']}::text = %(only_value)s)")
    return " ".join(clauses), params


def _label_scope(exclude: dict | None, only: dict | None) -> str:
    scope = ""
    if exclude:
        scope += f" excluding {exclude['dimension']}={exclude['value']}"
    if only:
        scope += f" only {only['dimension']}={only['value']}"
    return scope


def detect(conn: psycopg.Connection, *, as_of: datetime | None = None, window_minutes: int = 5,
           exclude: dict | None = None, only: dict | None = None) -> dict:
    """Score every signal. Returns {"as_of", "window", "signals": [...], "alarms": [...], "evidence": [...]}."""
    as_of = as_of or datetime.now(timezone.utc)
    clause, extra = _segment_clause(exclude, only)
    sql = DETECTOR_SQL.replace("{exclude_clause}", clause)
    params = {"as_of": as_of, "window_minutes": window_minutes, "baseline_minutes": BASELINE_MINUTES,
              "min_volume": MIN_VOLUME, "min_baseline_buckets": MIN_BASELINE_BUCKETS, **extra}
    rows = conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()

    window_from = (as_of - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
    window_to = as_of.isoformat(timespec="seconds")
    source = {"kind": "sql", "query_name": "detector", "params": {k: str(v) for k, v in params.items()}}
    signals, evidence = [], []
    for row in rows:
        spec = SIGNAL_BY_NAME[row["signal"]]
        z = row["z"] or 0.0
        wrong_way = spec.bad_direction == 0 or (z * spec.bad_direction) > 0
        alarm = bool(abs(z) > Z_THRESHOLD and wrong_way and row["effect_ok"] and row["volume_ok"] and row["baseline_ok"])
        label_scope = _label_scope(exclude, only)
        cur = Evidence(f"{spec.name} current window{label_scope}", row["current"], spec.unit, source,
                       "regress-mcp/detector", window_from, window_to)
        base = Evidence(f"{spec.name} baseline median (previous {BASELINE_MINUTES} min){label_scope}",
                        row["median"], spec.unit, source, "regress-mcp/detector")
        zev = Evidence(f"{spec.name} robust z-score{label_scope}", round(z, 2), "z", source,
                       "regress-mcp/detector", window_from, window_to)
        evidence += [cur, base, zev]
        signals.append({
            "signal": spec.name, "current": row["current"], "baseline_median": row["median"],
            "mad": row["mad"], "z": round(z, 2), "volume": row["volume"],
            "baseline_buckets": row["baseline_buckets"], "effect_ok": row["effect_ok"],
            "volume_ok": row["volume_ok"], "baseline_ok": row["baseline_ok"], "alarm": alarm,
            "evidence": {"current": cur.id, "baseline": base.id, "z": zev.id},
        })
    return {
        "as_of": window_to, "window": {"from": window_from, "to": window_to, "minutes": window_minutes},
        "exclude": exclude, "only": only, "signals": signals,
        "alarms": [s["signal"] for s in signals if s["alarm"]],
        "evidence": evidence, "sql": sql,
    }


def onset(conn: psycopg.Connection, signal: str, *, as_of: datetime | None = None, window_minutes: int = 5,
          lookback_minutes: int = 30) -> dict:
    """Earliest window end, scanning minute by minute, from which `signal` alarms continuously up to as_of."""
    as_of = as_of or datetime.now(timezone.utc)
    first = None
    for step in range(lookback_minutes, -1, -1):
        t = as_of - timedelta(minutes=step)
        alarming = signal in detect(conn, as_of=t, window_minutes=window_minutes)["alarms"]
        if alarming and first is None:
            first = t
        elif not alarming:
            first = None
    if first is None:
        return {"signal": signal, "onset": None, "evidence": []}
    ev = Evidence(f"{signal} first alarming window end (continuous to {as_of:%H:%M} UTC)", first.timestamp(),
                  "unix_ts", {"kind": "sql", "query_name": "detector", "params": {"scan": "1-minute steps",
                  "lookback_minutes": lookback_minutes, "window_minutes": window_minutes}},
                  "regress-mcp/detector.onset", first.isoformat(timespec="seconds"), first.isoformat(timespec="seconds"))
    return {"signal": signal, "onset": first.isoformat(timespec="seconds"), "evidence": [ev]}
