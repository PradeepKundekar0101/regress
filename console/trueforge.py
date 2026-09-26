"""The console's view of TrueForge: which session handles an incident, what it is waiting for, what it said,
and answering its pending approval. The token (if any) stays server-side."""

import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
WATCHER_STATE = ROOT / ".regress" / "watcher.json"


def base_url() -> str:
    return os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790").rstrip("/")


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    token = os.environ.get("TRUEFORGE_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=f"{base_url()}/api/v1", headers=headers, timeout=20, transport=transport)


SESSION_PAGE_SIZE, SESSION_PAGES = 25, 4


def session_for(incident_id: str, h: httpx.Client) -> str | None:
    try:
        mapped = json.loads(WATCHER_STATE.read_text()).get("sessions", {})
    except FileNotFoundError:
        mapped = {}
    if incident_id in mapped:
        return mapped[incident_id]
    # TrueForge pages sessions 25 at a time (a larger limit is a 400); look through the newest 100.
    token = None
    for _ in range(SESSION_PAGES):
        params = {"limit": SESSION_PAGE_SIZE} | ({"page_token": token} if token else {})
        page = h.get("/sessions", params=params).raise_for_status().json()
        for s in page.get("data", []):
            if (s.get("metadata") or {}).get("incident_id") == incident_id:
                return s["id"]
        token = (page.get("pagination") or {}).get("next_page_token")
        if not token:
            break
    return None


def _events(h: httpx.Client, session_id: str, turn_id: str) -> list[dict]:
    events, token = [], None
    while True:
        page = h.get(f"/sessions/{session_id}/turns/{turn_id}/events",
                     params={"page_token": token} if token else {}).raise_for_status().json()
        events += page.get("data", [])
        token = (page.get("pagination") or {}).get("next_page_token")
        if not token:
            return events


_REPORTS: dict[tuple, str | None] = {}


def _report(h: httpx.Client, session_id: str, turns: list[dict]) -> str | None:
    """The agent's full validated report: the longest main-thread message with a verdict.

    It is usually posted just before the approval pause, inside the turn rather than as its output, so it
    has to be read from events. Cached per session and turn state because the console polls.
    """
    key = (session_id, tuple((t["id"], (t.get("state") or {}).get("status")) for t in turns))
    if key not in _REPORTS:
        texts = [e.get("content") or "" for t in turns for e in _events(h, session_id, t["id"])
                 if e["type"] == "model.message" and e.get("thread_id") in (None, "main")]
        verdicts = [t for t in texts if "Verdict" in t]
        _REPORTS[key] = max(verdicts, key=len) if verdicts else None
    return _REPORTS[key]


def session_view(h: httpx.Client, session_id: str) -> dict:
    """Latest turn status, pending approvals with the exact call arguments, and the agent's last report."""
    turns = h.get(f"/sessions/{session_id}/turns").raise_for_status().json().get("data", [])
    if not turns:
        return {"session_id": session_id, "status": "starting", "pending": [], "report": None}
    latest = turns[-1]
    state = latest.get("state") or {}
    pending = []
    actions = [a for a in state.get("required_actions") or [] if a["type"] == "tool.approval_required"]
    if actions:
        calls = {c["id"]: c for e in _events(h, session_id, latest["id"]) if e["type"] == "model.message"
                 for c in e.get("tool_calls") or []}
        for action in actions:
            for ref in action["tool_calls"]:
                fn = calls.get(ref["id"], {}).get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                # Tools reached through TrueForge's call_tool wrapper carry the real name and input inside.
                tool = args.get("tool_name", fn.get("name"))
                pending.append({"thread_id": action["thread_id"], "tool_call_id": ref["id"],
                                "tool": tool, "arguments": args.get("input", args)})
    report = _report(h, session_id, turns)
    outputs = [c for c in (((t.get("state") or {}).get("output") or {}).get("content") for t in turns) if c]
    closing = outputs[-1] if outputs and outputs[-1] != report else None
    return {"session_id": session_id, "url": f"{base_url()}/sessions/{session_id}",
            "status": state.get("status"), "error": state.get("message"), "pending": pending, "report": report,
            "closing": closing}


def answer(h: httpx.Client, session_id: str, pending: dict, decision: str, reason: str | None) -> str:
    approval = {"status": decision, **({"reason": reason} if decision == "deny" and reason else {})}
    resp = h.post(f"/sessions/{session_id}/turns", json={"stream": False, "input": [{
        "type": "user.tool_approval", "thread_id": pending["thread_id"],
        "tool_call_id": pending["tool_call_id"], "approval": approval}]}).raise_for_status().json()
    return resp["data"]["id"]
