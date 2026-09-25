"""The only code that changes production: flip the prompt label or revert the route.

Both actions require a checkpointed incident whose frozen proposal matches the call exactly, and both
reconcile against the live state before writing, so a restart between approval and apply never
flips twice: live == from -> apply; live == to -> already applied; anything else -> conflict.
"""

from datetime import datetime, timezone

import psycopg
from langfuse import get_client

from regress_mcp.detector import detect
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
        store.transition(incident_id, "approved", "approved by a human in TrueForge", [])

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
        store.transition(incident_id, "approved", "approved by a human in TrueForge", [])

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


def verify_recovery(store: Store, incident_id: str, conn: psycopg.Connection, min_minutes: int = 1) -> dict:
    """Run the detector on traffic served by the restored version since the change. Replays never count."""
    incident = store.incident(incident_id)
    if incident["status"] != "applied":
        raise ActionRefused(f"{incident_id} is {incident['status']}; verification runs after apply")
    proposal = incident["proposal"]
    only = ({"dimension": "prompt_version", "value": proposal["to_version"]} if proposal["action"] == "rollback_execute"
            else {"dimension": "model", "value": proposal["to_model"]})
    applied_at = datetime.fromisoformat(incident["applied_at"])
    now = datetime.now(timezone.utc)
    minutes = max(min_minutes, int((now - applied_at).total_seconds() // 60) or min_minutes)
    minutes = min(minutes, 5)
    result = detect(conn, as_of=now, window_minutes=minutes, only=only, mask=store.incident_periods())
    store.add_evidence(incident_id, result["evidence"])
    watched = [a for a in (incident["signal"] or "").split(",") if a]
    rows = {s["signal"]: s for s in result["signals"]}
    volume = max((s["volume"] for s in result["signals"]), default=0)
    if not rows or not all(rows.get(a, {}).get("volume_ok") for a in watched or rows):
        return {"outcome": "pending", "reason": f"not enough fresh traffic on {only['dimension']}={only['value']} yet",
                "window_minutes": minutes, "volume": volume}
    still = [a for a in (watched or result["alarms"]) if a in result["alarms"]]
    ids = [rows[a]["evidence"]["z"] for a in (watched or rows) if a in rows]
    if still:
        store.transition(incident_id, "verify_failed", f"still alarming on fresh traffic: {still}", ids)
        return {"outcome": "verify_failed", "still_alarming": still, "evidence_ids": ids}
    store.transition(incident_id, "verified", f"signals back in band on fresh traffic ({only['dimension']}={only['value']})", ids)
    return {"outcome": "verified", "signals": {a: rows[a] for a in (watched or rows) if a in rows}, "evidence_ids": ids}
