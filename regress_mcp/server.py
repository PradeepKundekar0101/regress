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

from regress_mcp import actions, customer_view, detector, gates, localize as localize_mod, narrative, replay, slack, sources
from regress_mcp.store import Evidence, Store, TransitionError
from target import config
from target.prompts_registry import production_version

INSTRUCTIONS = """Investigate regressions in the Adopt.ai support bot.
Every number you report must come from an evidence id returned by these tools; write it as {{ev_id}}.
Flow: run_detector -> record_plan -> localize -> get_traces -> replay_generate -> (score in sandbox) ->
submit_replay_report -> check_gates -> validate_narrative -> request_approval (Slack) -> rollback_execute or
route_revert (needs human approval) -> verify_recovery -> post_update. NOT_LOCALIZED and INSUFFICIENT_DATA are valid endings.
capture_customer_view films the bot before approval (checkpointed) and after recovery (verified); it never blocks the flow."""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
ANALYSE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)
NOTIFY = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

mcp = MCPServer("regress", instructions=INSTRUCTIONS)
store = Store()

# Refusals the agent must read and act on. MCP hides the text of any other exception.
EXPECTED = (actions.ActionRefused, TransitionError, ValueError, KeyError, slack.SlackError)


def tool(annotations: ToolAnnotations):
    def register(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                try:
                    return fn(*args, **kwargs)
                except psycopg.OperationalError:
                    # The Supabase pooler occasionally drops a connection; one retry on a fresh one.
                    return fn(*args, **kwargs)
            except EXPECTED as exc:
                raise ToolError(str(exc).strip("'\"")) from exc
        return mcp.tool(annotations=annotations)(wrapper)
    return register


def _db() -> psycopg.Connection:
    return config.db_connect(autocommit=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _brief(evidence: list) -> list[dict]:
    items = [e if isinstance(e, dict) else e.as_dict() for e in evidence]
    return [{"id": e["id"], "label": e["label"], "value": narrative.fmt(e)} for e in items]


def _active_incident() -> str | None:
    """Evidence from read tools belongs to the open incident, so the narrator may cite it."""
    open_now = store.open_incidents()
    return open_now[0]["id"] if open_now else None


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
    store.add_evidence(_active_incident(), result["evidence"])
    return {k: v for k, v in result.items() if k != "evidence"}


@tool(READ)
def get_changes(minutes: int = 60) -> dict:
    """Prompt label moves and route changes in the last `minutes`, with their commits in the config repo."""
    with _db() as conn:
        result = sources.changes(conn, _now() - timedelta(minutes=minutes), _now())
    store.add_evidence(_active_incident(), result["evidence"])
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
    store.add_evidence(_active_incident(), result.get("evidence", []))
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
        result = detector.detect(conn, window_minutes=window_minutes, mask=store.incident_periods())
    store.add_evidence(None, result["evidence"])
    summary = [{k: s[k] for k in ("signal", "current", "baseline_median", "z", "volume", "alarm", "baseline_ok", "evidence")}
               for s in result["signals"]]
    out = {"window": result["window"], "alarms": result["alarms"], "signals": summary}
    if result["alarms"] and open_incident:
        existing = store.open_incidents()
        if existing:
            out["incident_id"], out["incident_status"] = existing[0]["id"], existing[0]["status"]
            out["note"] = "an incident is already open; continuing it"
        elif (draining := _draining(window_minutes)) is not None:
            out["suppressed_by"] = draining
            out["note"] = (f"alarms come only from {draining['retired_requests_in_window']} requests served by a config "
                           f"that is no longer live; live traffic (prompt v{draining['live']['prompt_version']}, "
                           f"{draining['live']['model']}) is in band")
        elif (blocked := _unchanged_since_last_close()) is not None:
            out["suppressed_by"] = blocked
            out["note"] = (f"still alarming, but {blocked['id']} already ended {blocked['status']} and nothing has "
                           "changed since; not opening a duplicate incident")
        else:
            ids = [s["evidence"][k] for s in result["signals"] if s["alarm"] for k in ("current", "baseline", "z")]
            out["incident_id"] = store.open_incident(",".join(result["alarms"]), ids, result["as_of"], window_minutes)
            out["incident_status"] = "detected"
    return out


def _draining(window_minutes: int) -> dict | None:
    """Alarms that come only from traffic served by a retired config are old traffic draining out of the
    window after a revert, not a new incident. Live traffic that still alarms (or re-applying a reverted
    change) is a real incident."""
    live = _live()
    with _db() as conn:
        retired = conn.execute(
            """select count(*) from requests where ts >= now() - make_interval(mins => %s)
               and source <> 'probe' and (prompt_version <> %s or model <> %s)""",
            (window_minutes, live["prompt_version"], live["model"])).fetchone()[0]
        if not retired:
            return None
        mask = store.incident_periods()
        for dim in ("prompt_version", "model"):
            run = detector.detect(conn, window_minutes=window_minutes, only={"dimension": dim, "value": live[dim]},
                                  mask=mask)
            # Healthy means judged healthy: too little live traffic yet is not evidence of recovery.
            if run["alarms"] or not all(sig["volume_ok"] for sig in run["signals"]):
                return None
    return {"live": live, "retired_requests_in_window": retired}


def _unchanged_since_last_close() -> dict | None:
    """Re-investigating identical facts is noise: after an incident ends without a fix, wait for a new change."""
    last = store.last_unresolved_close()
    if last is None:
        return None
    closed_at = datetime.fromisoformat(last["closed_at"])
    with _db() as conn:
        latest_change = conn.execute("select max(ts) from change_log").fetchone()[0]
    if latest_change is not None and latest_change > closed_at:
        return None
    if _now() - closed_at > timedelta(hours=2):
        return None
    return last


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
                                       alarms=incident["signal"].split(","),
                                       mask=store.incident_periods(exclude=incident_id))
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
    """Re-run real inputs under a baseline arm and a suspect arm, through the production model call.

    Call this directly (not from sandbox code: slow models can outlast the sandbox exec timeout). It returns a
    replay_id and a summary; your sandbox script then fetches the raw outputs with get_replay_outputs,
    scores them and calls submit_replay_report.
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
    result = replay.generate(store, incident_id, arms, inputs)
    return {k: v for k, v in result.items() if k != "outputs"}


@tool(READ)
def get_replay_outputs(replay_id: str) -> dict:
    """Raw replay outputs to score in the sandbox. Each output: trace_id, golden_id, arm ('baseline' or
    'suspect'), raw (the model's text), latency_ms, cost_usd, error, expected {escalate, refusal, citations,
    must_contain}."""
    row = store.replay(replay_id)
    return {"replay_id": replay_id, "incident_id": row["incident_id"], "arms": row["spec"]["arms"],
            "outputs": row["outputs"]}


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
        mask = store.incident_periods(exclude=incident_id)
        loc = localize_mod.localize(conn, as_of=as_of, window_minutes=window, alarms=incident["signal"].split(","),
                                    mask=mask)
        mine = next((c for c in loc["candidates"] if c["dimension"] == dimension and str(c["value"]) == value), None)
        onset = detector.onset(conn, mine["explains"] if mine else incident["signal"].split(","),
                               as_of=as_of, window_minutes=window, lookback_minutes=15, mask=mask)
        chg = sources.changes(conn, as_of - timedelta(minutes=60), _now())
        seg_total = conn.execute(
            f"select count(*) filter (where {dimension}::text = %s), count(*) from requests "
            "where ts >= %s - make_interval(mins => %s) and ts < %s and source <> 'probe'", (value, as_of, window, as_of)).fetchone()
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


@tool(NOTIFY)
def request_approval(incident_id: str, summary: str, linear_url: str | None = None) -> dict:
    """Ask for the human decision in Slack: your summary plus Approve/Reject buttons built from the frozen proposal.

    Call after check_gates returned checkpointed and before the gated tool. The summary is validator-rendered
    text (no {{ev_id}}). Pass the Linear issue URL if you filed one. Calling again updates the same message.
    """
    return slack.request_approval(store, incident_id, summary, linear_url)


@tool(NOTIFY)
def post_update(incident_id: str, text: str) -> dict:
    """Reply in the incident's Slack thread, or start one if none exists: outcomes, a denial's next branch,
    or a NOT_LOCALIZED / INSUFFICIENT_DATA ending. Text is validator-rendered (no {{ev_id}})."""
    return slack.post_update(store, incident_id, text)


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


# --- customer view -----------------------------------------------------------------------

CAPTURE_STATUS = {"before": "checkpointed", "after": "verified"}


@tool(ANALYSE)
def capture_customer_view(incident_id: str, phase: str) -> dict:
    """Film the live bot answering the fraud question, as a customer sees it, and record what it showed.

    phase "before": call after check_gates returned checkpointed, before the gated tool.
    phase "after": call after verify_recovery returned verified.
    Returns evidence ids for the specialist banner, the cited sources and the prompt version on screen.
    Never changes the incident. On failure returns captured=false with a reason: say so in one line and go on.
    """
    if phase not in CAPTURE_STATUS:
        raise ValueError(f"phase must be one of {sorted(CAPTURE_STATUS)}")
    status = store.incident(incident_id)["status"]
    if status != CAPTURE_STATUS[phase]:
        raise ValueError(f"{incident_id} is {status}; the {phase} view is captured when it is {CAPTURE_STATUS[phase]}")
    try:
        seen = customer_view.capture(incident_id, phase)
    except Exception as exc:  # The video is decoration, never a gate: any failure is reported, not raised.
        return {"captured": False, "reason": f"{type(exc).__name__}: {str(exc)[:300]}"}
    at = _now().isoformat(timespec="seconds")
    source = {"kind": "video", "path": str(seen["video"]), "screenshot": str(seen["screenshot"]),
              "url": seen["url"], "question": seen["question"], "prompt_version": seen["prompt_version"],
              "model": seen["model"], "citations": seen["citations"], "footer": seen["footer"]}
    by = "regress-mcp/customer_view"
    evidence = store.add_evidence(incident_id, [
        Evidence(f"customer view {phase}: specialist banner shown", float(seen["banner"]), "count", source, by, at, at),
        Evidence(f"customer view {phase}: sources cited ({', '.join(seen['citations']) or 'none'})",
                 float(len(seen["citations"])), "count", source, by, at, at),
        Evidence(f"customer view {phase}: prompt version on screen", seen["prompt_version"], "version", source, by, at, at),
    ])
    return {"captured": True, "phase": phase, "video": str(seen["video"]), "screenshot": str(seen["screenshot"]),
            "banner": seen["banner"], "citations": seen["citations"], "prompt_version": seen["prompt_version"],
            "model": seen["model"], "evidence": _brief(evidence)}


if __name__ == "__main__":
    import anyio

    port = int(os.environ.get("REGRESS_MCP_PORT", "8941"))
    anyio.run(lambda: mcp.run_streamable_http_async(host="127.0.0.1", port=port))
