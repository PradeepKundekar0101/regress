"""Localisation: which segment explains which alarm, by re-running the detector without it.

A segment "explains" an alarm when excluding that segment's requests makes the alarm vanish
(the Simpson's-exclusion check from RootCauseOS). Change dimensions (prompt version, model) are
candidate causes; the category drill-down says which customers were hit, not why.
"""

from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from regress_mcp.detector import detect

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


def localize(conn: psycopg.Connection, *, as_of: datetime, window_minutes: int, alarms: list[str]) -> dict:
    """For each change segment present in the window, which of `alarms` vanish without it."""
    candidates, evidence = [], []
    for dim, values in _segments(conn, as_of, window_minutes).items():
        for value in values:
            run = detect(conn, as_of=as_of, window_minutes=window_minutes,
                         exclude={"dimension": dim, "value": value})
            evidence += run["evidence"]
            rows = {s["signal"]: s for s in run["signals"]}
            # An alarm only counts as explained if the rest of the traffic still has the volume to judge.
            judged = [a for a in alarms if a in rows and rows[a]["volume_ok"] and rows[a]["baseline_ok"]]
            explained = [a for a in judged if not rows[a]["alarm"]]
            if not explained:
                continue
            candidates.append({
                "dimension": dim, "value": value, "explains": explained,
                "still_alarming_without_it": [a for a in judged if rows[a]["alarm"]],
                "inconclusive": [a for a in alarms if a not in judged],
                "z_after_exclusion": {a: {"z": rows[a]["z"], "evidence": rows[a]["evidence"]["z"]} for a in judged},
            })
    candidates.sort(key=lambda c: (-len(c["explains"]), c["dimension"] != "prompt_version"))
    explained_any = {a for c in candidates for a in c["explains"]}
    return {
        "as_of": as_of.isoformat(timespec="seconds"), "window_minutes": window_minutes, "alarms": alarms,
        "candidates": candidates,
        "unexplained": [a for a in alarms if a not in explained_any],
        "evidence": evidence,
    }
