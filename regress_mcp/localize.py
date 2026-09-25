"""Localisation: which segment explains which alarm, by re-running the detector without it.

A segment "explains" an alarm when excluding that segment's requests makes the alarm vanish
(the Simpson's-exclusion check from RootCauseOS). Change dimensions (prompt version, model) are
candidate causes; the category drill-down says which customers were hit, not why.
"""

from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from regress_mcp.detector import detect
from regress_mcp.store import Evidence

CHANGE_DIMENSIONS = ("prompt_version", "model")


def _segments(conn: psycopg.Connection, as_of: datetime, window_minutes: int) -> dict[str, list[str]]:
    sql = """select %(dim)s as dim, value from (
               select distinct {dim}::text as value from requests
               where ts >= %(as_of)s::timestamptz - make_interval(mins => %(w)s) and ts < %(as_of)s) s"""
    found = {}
    for dim in CHANGE_DIMENSIONS:
        rows = conn.cursor(row_factory=dict_row).execute(
            sql.format(dim=dim), {"dim": dim, "as_of": as_of, "w": window_minutes}).fetchall()
        found[dim] = sorted(r["value"] for r in rows)
    return found


COVERAGE_SHARE = 0.95


def _shares(conn: psycopg.Connection, dim: str, as_of: datetime, window_minutes: int) -> dict[str, float]:
    rows = conn.execute(
        f"""select {dim}::text, count(*)::float / sum(count(*)) over () from requests
            where ts >= %(as_of)s::timestamptz - make_interval(mins => %(w)s) and ts < %(as_of)s group by 1""",
        {"as_of": as_of, "w": window_minutes}).fetchall()
    return {value: share for value, share in rows}


def localize(conn: psycopg.Connection, *, as_of: datetime, window_minutes: int, alarms: list[str]) -> dict:
    """For each change segment present in the window, which of `alarms` vanish without it.

    When one segment is essentially the whole window (a regression that has been live longer than the
    window), there is nothing left to compare after excluding it; it then explains the alarms by
    coverage, and the causal proof rests on the onset, replay and competing-change gates.
    """
    candidates, evidence = [], []
    for dim, values in _segments(conn, as_of, window_minutes).items():
        shares = _shares(conn, dim, as_of, window_minutes)
        for value in values:
            run = detect(conn, as_of=as_of, window_minutes=window_minutes,
                         exclude={"dimension": dim, "value": value})
            evidence += run["evidence"]
            rows = {s["signal"]: s for s in run["signals"]}
            # An alarm only counts as explained if the rest of the traffic still has the volume to judge.
            judged = [a for a in alarms if a in rows and rows[a]["volume_ok"] and rows[a]["baseline_ok"]]
            explained = [a for a in judged if not rows[a]["alarm"]]
            inconclusive = [a for a in alarms if a not in judged]
            method = "exclusion"
            share = shares.get(str(value), 0.0)
            if not explained and share >= COVERAGE_SHARE and inconclusive:
                explained, inconclusive, method = inconclusive, [], "coverage"
            if not explained:
                continue
            share_ev = Evidence(f"share of window requests with {dim}={value}", round(share, 4), "ratio",
                                {"kind": "sql", "query_name": "localize.shares", "params": {"as_of": str(as_of),
                                 "window_minutes": window_minutes, "dimension": dim}}, "regress-mcp/localize")
            evidence.append(share_ev)
            candidates.append({
                "dimension": dim, "value": value, "explains": explained, "method": method,
                "window_share": {"value": round(share, 4), "evidence": share_ev.id},
                "still_alarming_without_it": [a for a in judged if rows[a]["alarm"]],
                "inconclusive": inconclusive,
                "z_after_exclusion": {a: {"z": rows[a]["z"], "evidence": rows[a]["evidence"]["z"]} for a in judged},
            })
    candidates.sort(key=lambda c: (c["method"] != "exclusion", -len(c["explains"]), c["dimension"] != "prompt_version"))
    explained_any = {a for c in candidates for a in c["explains"]}
    return {
        "as_of": as_of.isoformat(timespec="seconds"), "window_minutes": window_minutes, "alarms": alarms,
        "candidates": candidates,
        "unexplained": [a for a in alarms if a not in explained_any],
        "evidence": evidence,
    }
