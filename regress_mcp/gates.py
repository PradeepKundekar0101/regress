"""Correlation gates. A cause is claimed only when all four pass; otherwise the verdict is NOT_LOCALIZED.

1. Onset is within 10 minutes after the candidate change.
2. Replay of the same inputs reproduces the gap (>= 0.15 eval or >= 2x latency), verified.
3. Excluding the candidate segment removes its alarms; any alarm it leaves behind is explained
   by another segment that is no longer live (for example a change already rolled back).
4. No other unresolved change landed in the window.
"""

from datetime import datetime, timedelta

ONSET_MAX = timedelta(minutes=10)
MIN_EVAL_GAP = 0.15
MIN_LATENCY_RATIO = 2.0


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def candidate_change(candidate: dict, changes: list[dict], before: datetime | None) -> dict | None:
    """The most recent change that introduced the candidate segment (before `before`, if given)."""
    kind, to_value = {"prompt_version": ("prompt", f"v{candidate['value']}"),
                      "model": ("route", str(candidate["value"]))}[candidate["dimension"]]
    matching = [c for c in changes if c["kind"] == kind and c["to"] == to_value
                and (before is None or _ts(c["ts"]) <= before)]
    return matching[-1] if matching else None


def _undone(change: dict, changes: list[dict]) -> bool:
    """Part of a made-and-undone pair: reverted by a later change, or itself the revert of an earlier one."""
    same = [c for c in changes if c["target"] == change["target"] and c["change_id"] != change["change_id"]]
    reverted_later = any(_ts(c["ts"]) > _ts(change["ts"]) and c["to"] == change["from"] for c in same)
    is_revert = any(_ts(c["ts"]) < _ts(change["ts"]) and c["from"] == change["to"] and c["to"] == change["from"]
                    for c in same)
    return reverted_later or is_revert


def _names(items) -> str:
    return ", ".join(items) or "none"


def is_live(candidate: dict, live: dict) -> bool:
    if candidate["dimension"] == "prompt_version":
        return int(candidate["value"]) == live["prompt_version"]
    if candidate["dimension"] == "model":
        return candidate["value"] == live["model"]
    return True


def evaluate(*, candidate: dict, localization: dict, onset: str | None, changes: list[dict],
             replay_check: dict | None, live: dict) -> dict:
    gates = []
    onset_ts = _ts(onset) if onset else None
    change = candidate_change(candidate, changes, onset_ts)

    # Gate 1: onset follows the change within ONSET_MAX.
    if change is None or onset_ts is None:
        gates.append({"gate": 1, "name": "onset within 10 minutes of the change", "passed": False,
                      "detail": "no change introduced this segment" if change is None else "no sustained onset found",
                      "evidence": [change["evidence"]] if change else []})
    else:
        lag = onset_ts - _ts(change["ts"])
        gates.append({"gate": 1, "name": "onset within 10 minutes of the change",
                      "passed": timedelta(0) <= lag <= ONSET_MAX,
                      "detail": f"change at {change['ts']}, onset at {onset}, lag {int(lag.total_seconds())} s",
                      "evidence": [change["evidence"]]})

    # Gate 2: replay reproduces the gap on the same inputs, and the sandbox numbers were re-verified.
    if not replay_check or not replay_check.get("verified"):
        gates.append({"gate": 2, "name": "replay reproduces the gap", "passed": False,
                      "detail": "no verified replay" if not replay_check else f"replay report rejected: {replay_check.get('mismatches')}",
                      "evidence": []})
    else:
        eval_gap, lat = replay_check["eval_gap"], replay_check["latency_ratio"] or 0
        gates.append({"gate": 2, "name": "replay reproduces the gap",
                      "passed": eval_gap >= MIN_EVAL_GAP or lat >= MIN_LATENCY_RATIO,
                      "detail": f"eval gap {eval_gap:.3f} (need >= {MIN_EVAL_GAP}), latency ratio {lat:.2f}x "
                                f"(need >= {MIN_LATENCY_RATIO}x), coverage {replay_check['coverage']}",
                      "evidence": list(replay_check["evidence_ids"].values()) if isinstance(replay_check["evidence_ids"], dict) else []})

    # Gate 3: the exclusion re-run.
    mine = next((c for c in localization["candidates"]
                 if c["dimension"] == candidate["dimension"] and str(c["value"]) == str(candidate["value"])), None)
    if mine is None:
        gates.append({"gate": 3, "name": "excluding the segment removes the alarms", "passed": False,
                      "detail": "excluding this segment explains none of the alarms", "evidence": []})
    else:
        leftover, explained_elsewhere = mine["still_alarming_without_it"] + mine["inconclusive"], {}
        for alarm in leftover:
            other = next((c for c in localization["candidates"] if alarm in c["explains"] and c is not mine
                          and not is_live(c, live)), None)
            if other:
                explained_elsewhere[alarm] = f"{other['dimension']}={other['value']} (no longer live)"
        unexplained = [a for a in leftover if a not in explained_elsewhere]
        gates.append({"gate": 3, "name": "excluding the segment removes the alarms", "passed": not unexplained,
                      "detail": f"explains {_names(mine['explains'])} ({mine.get('method', 'exclusion')})"
                                + (f"; {_names(f'{a} by {why}' for a, why in explained_elsewhere.items())}" if explained_elsewhere else "")
                                + (f"; unexplained {_names(unexplained)}" if unexplained else ""),
                      "evidence": [v["evidence"] for v in mine["z_after_exclusion"].values()]})

    # Gate 4: no competing, unresolved change in the window.
    competing = []
    if change is not None:
        window_start = _ts(change["ts"]) - ONSET_MAX
        for c in changes:
            if c["change_id"] == change["change_id"] or c["by_regress"] or _ts(c["ts"]) < window_start:
                continue
            if _undone(c, changes):
                continue
            if c["target"] == change["target"] and _ts(c["ts"]) > _ts(change["ts"]):
                continue  # a later change to the same target is the fix, not a competitor
            competing.append(c)
    gates.append({"gate": 4, "name": "no competing change in the window", "passed": change is not None and not competing,
                  "detail": "competing: " + ", ".join(f"{c['kind']} {c['from']}->{c['to']} at {c['ts']}" for c in competing)
                            if competing else "no other unresolved change",
                  "evidence": [c["evidence"] for c in competing]})

    passed = all(g["passed"] for g in gates)
    verdict = ({"prompt_version": "LOCALIZED_PROMPT", "model": "LOCALIZED_ROUTE"}[candidate["dimension"]]
               if passed else "NOT_LOCALIZED")
    return {"candidate": candidate, "change": change, "gates": gates, "passed": passed, "verdict": verdict}


def proposal(candidate: dict, change: dict, prompt_name: str, route_name: str, blast: dict) -> dict:
    """The one change a human will be asked to approve. Frozen when the incident is checkpointed."""
    if candidate["dimension"] == "prompt_version":
        return {"action": "rollback_execute", "prompt": prompt_name, "label": "production",
                "from_version": int(candidate["value"]), "to_version": int(change["from"].lstrip("v")),
                "blast_radius": blast, "undo": f"flip the production label back to v{int(candidate['value'])}"}
    return {"action": "route_revert", "route": route_name, "from_model": str(candidate["value"]),
            "to_model": change["from"], "blast_radius": blast,
            "undo": f"set the {route_name} route back to {candidate['value']}"}
