"""Regress incident console: an operator view of what the agent found and a place to answer its one question.

    uv run uvicorn console.app:app --port 8100

Reads the incident store, Supabase telemetry and TrueForge sessions. Its only write is answering an
approval the agent already asked for, and it refuses unless the incident is checkpointed and the pending
call is exactly the frozen proposal.
"""

import json
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from console import trueforge
from target import config_repo
from regress_mcp import narrative
from regress_mcp.detector import detect
from regress_mcp.store import Store
from target import config

STATIC = Path(__file__).parent / "static"
GATED_TOOLS = {"rollback_execute", "route_revert"}
SERIES_SQL = """
select date_trunc('minute', ts) as minute,
  count(*) as requests,
  avg(eval_score) filter (where golden_id is not null and not provider_error) as eval_score,
  avg(format_valid::int) filter (where golden_id is not null and not provider_error) as format_valid,
  avg(escalation_correct::int) filter (where golden_id is not null and not provider_error) as escalation_correct,
  percentile_cont(0.95) within group (order by latency_ms) filter (where not provider_error) as latency_p95_ms,
  avg(cost_usd) filter (where not provider_error) as cost_per_request_usd,
  mode() within group (order by prompt_version) as prompt_version,
  mode() within group (order by model) as model
from requests where ts >= now() - make_interval(mins => %(minutes)s)
group by 1 order by 1
"""

app = FastAPI(title="Regress console")
state: dict = {}


def store() -> Store:
    return state.setdefault("store", Store())


def db():
    if "db" not in state:
        state["db"] = config.db_pool(max_size=2)
    return state["db"]


def tf_client() -> httpx.Client:
    return trueforge.client(state.get("tf_transport"))


def _gates(incident: dict) -> list[dict] | None:
    for t in reversed(incident["transitions"]):
        if t["to_status"] in ("checkpointed", "not_localized") and t["reason"].startswith("["):
            try:
                return json.loads(t["reason"])
            except json.JSONDecodeError:
                return None
    return None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/signals")
def signals(minutes: int = 60) -> dict:
    with db().connection() as conn:
        rows = conn.execute(SERIES_SQL, {"minutes": minutes}).fetchall()
        snapshot = detect(conn, mask=store().incident_periods())
    cols = ["minute", "requests", "eval_score", "format_valid", "escalation_correct", "latency_p95_ms",
            "cost_per_request_usd", "prompt_version", "model"]
    series = [{c: (v.isoformat() if isinstance(v, datetime) else float(v) if v is not None and c not in
                   ("prompt_version", "model") else v) for c, v in zip(cols, row)} for row in rows]
    current = {s["signal"]: {k: s[k] for k in ("current", "baseline_median", "mad", "z", "volume", "alarm", "baseline_ok", "baseline_buckets")}
               for s in snapshot["signals"]}
    return {"series": series, "detector": {"window": snapshot["window"], "alarms": snapshot["alarms"],
                                            "signals": current}}


@app.get("/api/incidents")
def incidents(limit: int = 30) -> list[dict]:
    with store()._conn() as conn:
        rows = conn.execute("select id, created_at, status, verdict, signal from incidents "
                            "order by created_at desc limit ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/incidents/{incident_id}")
def incident(incident_id: str) -> dict:
    try:
        inc = store().incident(incident_id)
    except KeyError:
        raise HTTPException(404, f"unknown incident {incident_id}")
    evidence = store().evidence(incident_id)
    session = None
    with tf_client() as h:
        try:
            sid = trueforge.session_for(incident_id, h)
            session = trueforge.session_view(h, sid) if sid else None
        except httpx.HTTPError as exc:
            session = {"unavailable": str(exc)}
    with db().connection() as conn:
        commits = conn.execute(
            "select ts, kind, from_value, to_value, actor, commit_sha from change_log "
            "where actor like %s order by ts", (f"%{incident_id}%",)).fetchall()
    repo = config.optional_env("CONFIG_REPO")
    return {
        **inc,
        "gates": _gates(inc),
        "session": session,
        "actions": [{"ts": c[0].isoformat(), "kind": c[1], "from": c[2], "to": c[3], "actor": c[4], "sha": c[5],
                     "commit_url": f"https://github.com/{repo}/commit/{c[5]}" if repo and c[5] else None}
                    for c in commits],
        "evidence": [{"id": e["id"], "label": e["label"], "value": narrative.fmt(e), "kind": e["source"].get("kind"),
                      "computed_by": e["computed_by"]} for e in evidence.values()],
    }


@app.get("/api/incidents/{incident_id}/evidence/{evidence_id}")
def evidence_detail(incident_id: str, evidence_id: str) -> dict:
    ev = store().evidence(incident_id).get(evidence_id)
    if ev is None:
        raise HTTPException(404, f"unknown evidence {evidence_id}")
    host = config.optional_env("LANGFUSE_BASE_URL")
    trace_ids = ev["source"].get("trace_ids") or []
    return {**ev, "formatted": narrative.fmt(ev),
            "trace_links": [f"{host}/trace/{t}" for t in trace_ids[:20]] if host else []}


@app.get("/api/changes")
def changes(minutes: int = 180) -> list[dict]:
    repo = config.optional_env("CONFIG_REPO")
    with db().connection() as conn:
        rows = conn.execute(
            "select ts, kind, target, from_value, to_value, actor, commit_sha from change_log "
            "where ts >= now() - make_interval(mins => %s) order by ts desc limit 30", (minutes,)).fetchall()
    return [{"ts": r[0].isoformat(), "kind": r[1], "target": r[2], "from": r[3], "to": r[4], "actor": r[5],
             "commit_url": f"https://github.com/{repo}/commit/{r[6]}" if repo and r[6] else None} for r in rows]


_DIFFS: dict[str, dict] = {}


@app.get("/api/commits/{sha}")
def commit(sha: str) -> dict:
    """A commit's message and per-file patches from the chatbot's repo (immutable, so cached)."""
    if sha not in _DIFFS:
        try:
            diff = config_repo.commit_diff(sha)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"GitHub unavailable: {exc}")
        if diff is None:
            raise HTTPException(404, "CONFIG_REPO is not set")
        _DIFFS[sha] = diff
    return _DIFFS[sha]


class Decision(BaseModel):
    decision: str = Field(pattern="^(allow|deny)$")
    reason: str | None = Field(default=None, max_length=500)


def _proposal_args(proposal: dict) -> tuple[str, dict]:
    keys = {"rollback_execute": ("prompt", "label", "from_version", "to_version"),
            "route_revert": ("route", "from_model", "to_model")}[proposal["action"]]
    return proposal["action"], {k: proposal[k] for k in keys}


@app.post("/api/incidents/{incident_id}/decision")
def decide(incident_id: str, body: Decision) -> dict:
    try:
        inc = store().incident(incident_id)
    except KeyError:
        raise HTTPException(404, f"unknown incident {incident_id}")
    if inc["status"] != "checkpointed" or not inc["proposal"]:
        raise HTTPException(409, f"{incident_id} is {inc['status']}; only a checkpointed incident awaits a decision")
    with tf_client() as h:
        sid = trueforge.session_for(incident_id, h)
        if sid is None:
            raise HTTPException(409, "no TrueForge session is linked to this incident")
        view = trueforge.session_view(h, sid)
        gated = [p for p in view["pending"] if p["tool"] in GATED_TOOLS]
        if len(gated) != 1:
            raise HTTPException(409, f"expected exactly one pending gated call, found {len(gated)}")
        pending = gated[0]
        tool, expected = _proposal_args(inc["proposal"])
        actual = {k: pending["arguments"].get(k) for k in expected}
        if pending["tool"] != tool or actual != expected or pending["arguments"].get("incident_id") != incident_id:
            raise HTTPException(409, f"the pending call does not match the frozen proposal: {pending['tool']} {actual}")
        turn_id = trueforge.answer(h, sid, pending, body.decision, body.reason)
    return {"ok": True, "decision": body.decision, "turn_id": turn_id, "session_url": view["url"]}
