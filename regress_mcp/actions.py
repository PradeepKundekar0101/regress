"""The only code that changes production: flip the prompt label or revert the route.

Both actions require a checkpointed incident whose frozen proposal matches the call exactly, and both
reconcile against the live state before writing, so a restart between approval and apply never
flips twice: live == from -> apply; live == to -> already applied; anything else -> conflict.
"""

from datetime import datetime, timezone

import psycopg
from langfuse import get_client

from regress_mcp import sources
from regress_mcp.detector import MIN_VOLUME, SIGNAL_BY_NAME, detect
from regress_mcp.store import Evidence, Store
from target import config, config_repo
from target.prompts_registry import production_version

ALLOWED_PROMPTS = {"adopt-support"}
ALLOWED_ROUTES = {"support"}


class ActionRefused(RuntimeError):
    pass


def _check_proposal(store: Store, incident_id: str, action: str, args: dict) -> dict:
    incident = store.incident(incident_id)
    proposal = incident["proposal"]
    if incident["status"] in ("applied", "verified", "verify_failed"):
        return incident  # idempotent re-call after apply
    if incident["status"] not in ("checkpointed", "approved"):
        raise ActionRefused(f"{incident_id} is {incident['status']}; only a checkpointed incident can be applied")
    if not proposal or proposal["action"] != action:
        raise ActionRefused(f"{incident_id} has no {action} proposal")
    mismatched = {k: (v, proposal.get(k)) for k, v in args.items() if proposal.get(k) != v}
    if mismatched:
        raise ActionRefused(f"arguments differ from the frozen proposal: {mismatched}")
    return incident


def approval_reason(store: Store, incident_id: str) -> str:
    """The approved transition's reason: who decided, from Slack ("@ana") or the console, when recorded."""
    decided_by = (store.notification(incident_id) or {}).get("decided_by")
    return f"approved by {decided_by}" if decided_by else "approved by a human in TrueForge"


def _log_change(conn, incident_id: str, kind: str, target: str, frm: str, to: str, sha: str | None) -> None:
    conn.execute(
        """insert into change_log (kind, target, from_value, to_value, actor, commit_sha, note)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (kind, target, frm, to, f"regress ({incident_id})", sha, "approved rollback"),
    )


def rollback_prompt(store: Store, incident_id: str, prompt: str, label: str,
                    from_version: int, to_version: int) -> dict:
    if prompt not in ALLOWED_PROMPTS or label != "production":
        raise ActionRefused(f"{prompt}:{label} is not on the allowlist")
    incident = _check_proposal(store, incident_id, "rollback_execute",
                               {"prompt": prompt, "label": label, "from_version": from_version, "to_version": to_version})
    if incident["status"] in ("applied", "verified", "verify_failed"):
        return {"outcome": "already_applied", "incident": incident}
    if incident["status"] == "checkpointed":
        store.transition(incident_id, "approved", approval_reason(store, incident_id), [])

    langfuse = get_client()
    live = production_version(langfuse, prompt)
    live_ev = Evidence(f"live production version of {prompt} before apply", live, "version",
                       {"kind": "langfuse", "prompt": prompt, "label": label}, "regress-mcp/actions")
    store.add_evidence(incident_id, [live_ev])
    if live == to_version:
        store.transition(incident_id, "applied", f"label already on v{to_version}; not flipping again",
                         [live_ev.id], applied_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return {"outcome": "already_applied_reconciled", "live_version": live}
    if live != from_version:
        store.transition(incident_id, "conflict", f"label is on v{live}, expected v{from_version}; someone else changed it",
                         [live_ev.id])
        return {"outcome": "conflict", "live_version": live}

    langfuse.update_prompt(name=prompt, version=to_version, new_labels=[label])
    sha = config_repo.record_prompt(prompt, to_version, f"Regress rollback ({incident_id}): {prompt} v{from_version} -> v{to_version}")
    with config.db_connect() as conn:
        _log_change(conn, incident_id, "prompt", f"{prompt}:{label}", f"v{from_version}", f"v{to_version}", sha)
    applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.transition(incident_id, "applied", f"{label} label moved v{from_version} -> v{to_version}", [live_ev.id],
                     applied_at=applied_at)
    return {"outcome": "applied", "from_version": from_version, "to_version": to_version, "commit": sha,
            "applied_at": applied_at}


def revert_route(store: Store, incident_id: str, route: str, from_model: str, to_model: str) -> dict:
    if route not in ALLOWED_ROUTES:
        raise ActionRefused(f"route {route} is not on the allowlist")
    incident = _check_proposal(store, incident_id, "route_revert",
                               {"route": route, "from_model": from_model, "to_model": to_model})
    if incident["status"] in ("applied", "verified", "verify_failed"):
        return {"outcome": "already_applied", "incident": incident}
    if incident["status"] == "checkpointed":
        store.transition(incident_id, "approved", approval_reason(store, incident_id), [])

    with config.db_connect() as conn:
        live = conn.execute("select model from routes where name = %s for update", (route,)).fetchone()[0]
        live_ev = Evidence(f"live model on route {route} before apply", None, "model",
                           {"kind": "sql", "query": "select model from routes where name = %s", "value": live},
                           "regress-mcp/actions")
        store.add_evidence(incident_id, [live_ev])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if live == to_model:
            store.transition(incident_id, "applied", f"route already on {to_model}; not changing again", [live_ev.id],
                             applied_at=now)
            return {"outcome": "already_applied_reconciled", "live_model": live}
        if live != from_model:
            store.transition(incident_id, "conflict", f"route is on {live}, expected {from_model}", [live_ev.id])
            return {"outcome": "conflict", "live_model": live}
        conn.execute("update routes set model = %s, updated_at = now(), updated_by = %s where name = %s",
                     (to_model, f"regress ({incident_id})", route))
        sha = config_repo.record_route(route, to_model, f"Regress revert ({incident_id}): {route} {from_model} -> {to_model}")
        _log_change(conn, incident_id, "route", route, from_model, to_model, sha)
    store.transition(incident_id, "applied", f"route {route} {from_model} -> {to_model}", [live_ev.id], applied_at=now)
    return {"outcome": "applied", "from_model": from_model, "to_model": to_model, "commit": sha, "applied_at": now}


def _detection_baselines(store: Store, incident: dict) -> dict[str, float]:
    """Baseline medians recorded when the incident was detected (clean at that moment, by construction)."""
    evidence = store.evidence(incident["id"])
    ids = incident["transitions"][0]["evidence_ids"]
    out = {}
    for eid in ids:
        ev = evidence.get(eid)
        if ev and " baseline median" in ev["label"] and ev["value"] is not None:
            out[ev["label"].split(" baseline median")[0]] = ev["value"]
    return out


def _within_effect(signal: str, current: float, baseline: float) -> bool:
    spec = SIGNAL_BY_NAME[signal]
    if spec.kind == "rate":
        return abs(current - baseline) < spec.min_effect
    return baseline > 0 and 1 / spec.min_effect < current / baseline < spec.min_effect


def verify_recovery(store: Store, incident_id: str, conn: psycopg.Connection, min_minutes: int = 1) -> dict:
    """Judge fresh traffic served by the restored version since the change. Replays never count.

    Each alarming signal is judged against the live detector baseline when that baseline is trustworthy
    (enough clean history), otherwise against the baseline recorded at detection. A thin baseline never
    counts as "in band": the detector reports no alarm then, which is not evidence of recovery.
    """
    incident = store.incident(incident_id)
    if incident["status"] != "applied":
        raise ActionRefused(f"{incident_id} is {incident['status']}; verification runs after apply")
    proposal = incident["proposal"]
    only = ({"dimension": "prompt_version", "value": proposal["to_version"]} if proposal["action"] == "rollback_execute"
            else {"dimension": "model", "value": proposal["to_model"]})
    applied_at = datetime.fromisoformat(incident["applied_at"])
    now = datetime.now(timezone.utc)
    minutes = min(5, max(min_minutes, int((now - applied_at).total_seconds() // 60)))
    result = detect(conn, as_of=now, window_minutes=minutes, only=only, mask=store.incident_periods())
    store.add_evidence(incident_id, result["evidence"])
    rows = {s["signal"]: s for s in result["signals"]}
    watched = [a for a in (incident["signal"] or "").split(",") if a]

    fresh = sources.window_stats(conn, applied_at, now, only["dimension"])
    store.add_evidence(incident_id, fresh["evidence"])
    seg = next((x for x in fresh["segments"] if str(x["segment"]) == str(only["value"])), None)
    volume = int(seg["golden_requests"]) if seg else 0
    if volume < MIN_VOLUME:
        return {"outcome": "pending", "reason": f"{volume} fresh golden requests on {only['dimension']}={only['value']} "
                f"since the change; need {MIN_VOLUME}", "volume": volume}

    recorded = _detection_baselines(store, incident)
    verdicts, ids = {}, []
    for signal in watched:
        row = rows.get(signal)
        if row and row["baseline_ok"] and row["volume_ok"]:
            verdicts[signal] = {"ok": not row["alarm"], "against": "live baseline", "current": row["current"],
                                "baseline": row["baseline_median"], "z": row["z"]}
            ids.append(row["evidence"]["z"])
        elif signal in recorded and seg and seg.get(signal) is not None:
            verdicts[signal] = {"ok": _within_effect(signal, seg[signal], recorded[signal]),
                                "against": "baseline recorded at detection", "current": seg[signal],
                                "baseline": recorded[signal]}
            ids.append(seg["evidence"].get(signal))
        else:
            verdicts[signal] = {"ok": None, "against": "no baseline available"}
    unknown = [a for a, v in verdicts.items() if v["ok"] is None]
    if unknown:
        return {"outcome": "pending", "reason": f"no usable baseline yet for {unknown}", "signals": verdicts}
    still = [a for a, v in verdicts.items() if not v["ok"]]
    ids = [i for i in ids if i]
    if still:
        store.transition(incident_id, "verify_failed", f"still degraded on fresh traffic: {still}", ids)
        return {"outcome": "verify_failed", "still_alarming": still, "signals": verdicts, "evidence_ids": ids}
    against = sorted({v["against"] for v in verdicts.values()})
    store.transition(incident_id, "verified", f"signals back in band on fresh traffic ({only['dimension']}={only['value']}; "
                     f"judged against {', '.join(against)})", ids)
    return {"outcome": "verified", "signals": verdicts, "evidence_ids": ids}
