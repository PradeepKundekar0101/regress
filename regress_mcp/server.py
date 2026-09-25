"""regress-mcp: the tools the Regress agent uses to investigate and fix LLM app regressions.

Reads and analysis are free. `rollback_execute` and `route_revert` are the only tools that change
production; TrueForge gates them with require_approval_for_tools, and they also refuse to run unless
the incident is checkpointed and the call matches the frozen proposal.

Run: uv run python -m regress_mcp.server   (streamable HTTP on http://127.0.0.1:8941/mcp)
"""

import functools
import json
import os
from datetime import datetime, timedelta, timezone

import psycopg
from langfuse import get_client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from regress_mcp import actions, detector, gates, localize as localize_mod, narrative, replay, sources
from regress_mcp.store import Store, TransitionError
from target import config
from target.prompts_registry import production_version

INSTRUCTIONS = """Investigate regressions in the Adopt.ai support bot.
Every number you report must come from an evidence id returned by these tools; write it as {{ev_id}}.
Flow: run_detector -> record_plan -> localize -> get_traces -> replay_generate -> (score in sandbox) ->
submit_replay_report -> check_gates -> validate_narrative -> rollback_execute or route_revert (needs human
approval) -> verify_recovery. NOT_LOCALIZED and INSUFFICIENT_DATA are valid endings."""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
ANALYSE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

mcp = MCPServer("regress", instructions=INSTRUCTIONS)
store = Store()

# Refusals the agent must read and act on. MCP hides the text of any other exception.
EXPECTED = (actions.ActionRefused, TransitionError, ValueError, KeyError)


def tool(annotations: ToolAnnotations):
    def register(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except EXPECTED as exc:
                raise ToolError(str(exc).strip("'\"")) from exc
        return mcp.tool(annotations=annotations)(wrapper)
    return register


def _db() -> psycopg.Connection:
    return psycopg.connect(config.env("DATABASE_URL"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _brief(evidence: list) -> list[dict]:
    items = [e if isinstance(e, dict) else e.as_dict() for e in evidence]
    return [{"id": e["id"], "label": e["label"], "value": narrative.fmt(e)} for e in items]


def _live() -> dict:
    with _db() as conn:
        model = sources.route(conn)["model"]
    return {"prompt_version": production_version(get_client(), config.prompt_name()), "model": model}


# --- reads -------------------------------------------------------------------------------


@tool(READ)
def get_window_stats(minutes: int = 15, group_by: str | None = None) -> dict:
    """Signals over the last `minutes`, optionally grouped by prompt_version, model, category or source."""
    with _db() as conn:
        result = sources.window_stats(conn, _now() - timedelta(minutes=minutes), _now(), group_by)
    store.add_evidence(None, result["evidence"])
    return {k: v for k, v in result.items() if k != "evidence"}


@tool(READ)
def get_changes(minutes: int = 60) -> dict:
    """Prompt label moves and route changes in the last `minutes`, with their commits in the config repo."""
    with _db() as conn:
        result = sources.changes(conn, _now() - timedelta(minutes=minutes), _now())
    store.add_evidence(None, result["evidence"])
    return {k: v for k, v in result.items() if k != "evidence"}


@tool(READ)
def get_traces(minutes: int = 30, prompt_version: int | None = None, model: str | None = None,
               category: str | None = None, limit: int = 30) -> dict:
    """Recent real golden-set requests (one per question): trace ids and inputs to replay."""
    with _db() as conn:
        return sources.traces(conn, _now() - timedelta(minutes=minutes), _now(), prompt_version=prompt_version,
                              model=model, category=category, limit=limit)


@tool(READ)
def get_user_signals(minutes: int = 15) -> dict:
    """Thumbs-down and talk-to-human events from PostHog: the customer impact."""
    result = sources.user_signals(minutes)
    store.add_evidence(None, result.get("evidence", []))
    return {k: v for k, v in result.items() if k != "evidence"}


@tool(READ)
def get_route() -> dict:
    """The model currently serving the support route."""
    with _db() as conn:
        return sources.route(conn)


@tool(READ)
def get_prompt(version: int | None = None) -> dict:
    """A prompt version's text and config (the production one if no version is given)."""
    return sources.prompt(version)


@tool(READ)
def get_incident(incident_id: str) -> dict:
    """An incident's status, frozen proposal, transitions and evidence."""
    incident = store.incident(incident_id)
    return {**incident, "evidence": _brief(list(store.evidence(incident_id).values()))}


@tool(READ)
def get_evidence(incident_id: str, evidence_ids: list[str]) -> list[dict]:
    """Full provenance (SQL, parameters, trace ids) for evidence rows."""
    evidence = store.evidence(incident_id)
    return [evidence[e] for e in evidence_ids if e in evidence]


# --- analysis ----------------------------------------------------------------------------


@tool(ANALYSE)
def run_detector(window_minutes: int = 5, open_incident: bool = True) -> dict:
    """Robust z-score of every signal in the last window vs the previous 2 hours. Opens an incident on alarm."""
    with _db() as conn:
        result = detector.detect(conn, window_minutes=window_minutes)
    store.add_evidence(None, result["evidence"])
    summary = [{k: s[k] for k in ("signal", "current", "baseline_median", "z", "volume", "alarm", "evidence")}
               for s in result["signals"]]
    out = {"window": result["window"], "alarms": result["alarms"], "signals": summary}
    if result["alarms"] and open_incident:
        existing = store.open_incidents()
        if existing:
            out["incident_id"], out["incident_status"] = existing[0]["id"], existing[0]["status"]
            out["note"] = "an incident is already open; continuing it"
        else:
            ids = [s["evidence"][k] for s in result["signals"] if s["alarm"] for k in ("current", "baseline", "z")]
            out["incident_id"] = store.open_incident(",".join(result["alarms"]), ids, result["as_of"], window_minutes)
            out["incident_status"] = "detected"
    return out


@tool(ANALYSE)
def record_plan(incident_id: str, plan: str) -> dict:
    """Record the investigation plan (detected -> planned)."""
    return store.transition(incident_id, "planned", f"plan: {plan[:2000]}", [])


@tool(ANALYSE)
def localize(incident_id: str) -> dict:
    """Which change segment (prompt version, model) explains which alarm, by the exclusion re-run."""
    incident = store.incident(incident_id)
    as_of = datetime.fromisoformat(incident["detected_as_of"])
    with _db() as conn:
        result = localize_mod.localize(conn, as_of=as_of, window_minutes=incident["window_minutes"],
                                       alarms=incident["signal"].split(","))
        by_category = sources.window_stats(conn, as_of - timedelta(minutes=incident["window_minutes"]), as_of, "category")
    store.add_evidence(incident_id, result["evidence"] + by_category["evidence"])
    worst = sorted((s for s in by_category["segments"] if s.get("eval_score") is not None),
                   key=lambda s: s["eval_score"])[:4]
    return {"alarms": result["alarms"], "candidates": result["candidates"], "unexplained": result["unexplained"],
            "worst_categories": [{"category": s["segment"], "eval_score": s["eval_score"],
                                  "requests": s["requests"], "evidence": s["evidence"].get("eval_score")} for s in worst]}


@tool(ANALYSE)
def replay_generate(incident_id: str, baseline_prompt_version: int, baseline_model: str,
                    suspect_prompt_version: int, suspect_model: str, trace_ids: list[str] | None = None,
                    limit: int = 20) -> dict:
    """Re-run real inputs under a baseline arm and a suspect arm. Returns raw outputs for your sandbox code to score.

    Each output has trace_id, golden_id, arm ('baseline' or 'suspect'), raw (the model's text), latency_ms,
    cost_usd, error and expected {escalate, refusal, citations, must_contain}.
    """
    incident = store.incident(incident_id)
    if incident["status"] not in ("planned", "replayed"):
        raise ValueError(f"{incident_id} is {incident['status']}; replay runs after record_plan")
    with _db() as conn:
        if trace_ids:
            rows = conn.execute("select distinct on (golden_id) trace_id, golden_id, question from requests "
                                "where trace_id = any(%s) and golden_id is not null", (trace_ids,)).fetchall()
        else:
            recent = sources.traces(conn, _now() - timedelta(minutes=60), _now(), limit=limit)["traces"]
            rows = [(t["trace_id"], t["golden_id"], t["question"]) for t in recent]
    inputs = [{"trace_id": t, "golden_id": g, "question": q} for t, g, q in rows][:limit]
    arms = [{"name": "baseline", "prompt_version": baseline_prompt_version, "model": baseline_model},
            {"name": "suspect", "prompt_version": suspect_prompt_version, "model": suspect_model}]
    return replay.generate(store, incident_id, arms, inputs)


@tool(ANALYSE)
def submit_replay_report(incident_id: str, replay_id: str, report: dict) -> dict:
    """Submit your sandbox scoring of a replay. It is re-scored deterministically; mismatches are rejected.

    report = {"arms": {"baseline": {"n", "eval_score_mean", "format_valid_rate", "latency_p50_ms"},
                       "suspect": {...}}}
    """
    check = replay.verify_report(store, replay_id, report, "baseline", "suspect")
    store.add_evidence(incident_id, check["evidence"])
    store.set_replay_verification(replay_id, {k: v for k, v in check.items() if k != "evidence"})
    if check["verified"] and store.incident(incident_id)["status"] == "planned":
        store.transition(incident_id, "replayed", f"replay {replay_id} verified", list(check["evidence_ids"].values()))
    return {k: v for k, v in check.items() if k != "evidence"} | {"evidence": _brief(check["evidence"])}


@tool(ANALYSE)
def check_gates(incident_id: str, dimension: str, value: str) -> dict:
    """Run the four correlation gates for a candidate cause (dimension prompt_version or model).

    All inputs are recomputed here from stored facts. Pass -> checkpointed with a frozen proposal.
    Fail -> not_localized; the incident cannot reach approval.
    """
    incident = store.incident(incident_id)
    if incident["status"] != "replayed":
        raise ValueError(f"{incident_id} is {incident['status']}; gates run after a verified replay")
    candidate = {"dimension": dimension, "value": value}
    as_of, window = datetime.fromisoformat(incident["detected_as_of"]), incident["window_minutes"]
    with _db() as conn:
        loc = localize_mod.localize(conn, as_of=as_of, window_minutes=window, alarms=incident["signal"].split(","))
        mine = next((c for c in loc["candidates"] if c["dimension"] == dimension and str(c["value"]) == value), None)
        onset = detector.onset(conn, (mine["explains"] if mine else incident["signal"].split(","))[0],
                               as_of=as_of, window_minutes=window)
        chg = sources.changes(conn, as_of - timedelta(minutes=60), _now())
        seg_total = conn.execute(
            f"select count(*) filter (where {dimension}::text = %s), count(*) from requests "
            "where ts >= %s - make_interval(mins => %s) and ts < %s", (value, as_of, window, as_of)).fetchone()
    store.add_evidence(incident_id, loc["evidence"] + onset["evidence"] + chg["evidence"])
    replay_row = store.latest_verified_replay(incident_id)
    result = gates.evaluate(candidate=candidate, localization=loc, onset=onset["onset"], changes=chg["changes"],
                            replay_check=replay_row["verification"] if replay_row else None, live=_live())
    summary = [{"gate": g["gate"], "passed": g["passed"], "detail": g["detail"]} for g in result["gates"]]
    if result["passed"]:
        blast = {"affected_requests_in_window": seg_total[0], "requests_in_window": seg_total[1],
                 "scope": f"one {'prompt label' if dimension == 'prompt_version' else 'route'}"}
        prop = gates.proposal(candidate, result["change"], config.prompt_name(), config.ROUTE_NAME, blast)
        prop["change_commit"] = result["change"].get("commit")
        store.transition(incident_id, "checkpointed", json.dumps(summary), [], verdict=result["verdict"], proposal=prop)
        return {"verdict": result["verdict"], "gates": summary, "proposal": prop, "status": "checkpointed"}
    store.transition(incident_id, "not_localized", json.dumps(summary), [], verdict="NOT_LOCALIZED")
    return {"verdict": "NOT_LOCALIZED", "gates": summary, "status": "not_localized"}


@tool(ANALYSE)
def validate_narrative(incident_id: str, text: str) -> dict:
    """Check a report: numbers only as {{ev_id}} placeholders from this incident. Returns rendered text or reasons.

    After one rejected retry, call again with use_template semantics by passing text="TEMPLATE".
    """
    evidence = store.evidence(incident_id)
    if text.strip() == "TEMPLATE":
        return {"accepted": True, "rendered": narrative.template(store.incident(incident_id), None, evidence),
                "fallback": True}
    return narrative.validate(text, evidence)


# --- decisions and the gated writes ------------------------------------------------------


@tool(ANALYSE)
def record_decision(incident_id: str, decision: str, reason: str) -> dict:
    """Record a human denial, or end the incident as not_localized / insufficient_data."""
    if decision not in ("denied", "not_localized", "insufficient_data"):
        raise ValueError("decision must be denied, not_localized or insufficient_data")
    verdict = {"not_localized": "NOT_LOCALIZED", "insufficient_data": "INSUFFICIENT_DATA"}.get(decision)
    fields = {"verdict": verdict} if verdict else {}
    return store.transition(incident_id, decision, reason, [], **fields)


@tool(WRITE)
def rollback_execute(incident_id: str, prompt: str, label: str, from_version: int, to_version: int) -> dict:
    """CHANGES PRODUCTION. Move the prompt label back to the previous version. Requires human approval.

    Arguments must equal the incident's frozen proposal. Safe to call again after a restart: it reads
    the live label first and never flips twice.
    """
    return actions.rollback_prompt(store, incident_id, prompt, label, from_version, to_version)


@tool(WRITE)
def route_revert(incident_id: str, route: str, from_model: str, to_model: str) -> dict:
    """CHANGES PRODUCTION. Point the route back at the previous model. Requires human approval.

    Arguments must equal the incident's frozen proposal. Idempotent across restarts.
    """
    return actions.revert_route(store, incident_id, route, from_model, to_model)


@tool(ANALYSE)
def verify_recovery(incident_id: str) -> dict:
    """After apply: run the detector on fresh traffic served by the restored version. Replays never count."""
    with _db() as conn:
        return actions.verify_recovery(store, incident_id, conn)


if __name__ == "__main__":
    import anyio

    port = int(os.environ.get("REGRESS_MCP_PORT", "8941"))
    anyio.run(lambda: mcp.run_streamable_http_async(host="127.0.0.1", port=port))
