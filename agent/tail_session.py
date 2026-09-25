"""Follow a TrueForge session: tool calls, subagents, sandbox runs, approval pauses, final messages.

    uv run python -m agent.tail_session <session_id>          # follow until the latest turn is done or paused
"""

import json
import os
import sys
import time

import httpx

TRUEFORGE = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790").rstrip("/") + "/api/v1"
QUIET = {"message.delta", "reasoning.delta", "tool.call.delta", "model.message.delta"}


def describe(e: dict) -> str | None:
    t, thread = e["type"], e.get("thread_id") or "main"
    tag = "" if thread == "main" else f"[{thread[-6:]}] "
    if t == "model.message":
        calls = e.get("tool_calls") or []
        if calls:
            names = []
            for c in calls:
                fn = c["function"]
                args = fn.get("arguments", "")
                brief = args if len(args) < 140 else args[:140] + "..."
                names.append(f"{fn['name']}({brief})")
            return f"{tag}-> " + "; ".join(names)
        text = (e.get("content") or "").strip()
        return f"{tag}says: {text[:1500]}" if text else None
    if t == "tool.response":
        content = e.get("content") or ""
        content = content if isinstance(content, str) else json.dumps(content)
        return f"{tag}   <- {content[:260]}"
    if t in ("tool.approval_required", "tool.response_required"):
        return f"{tag}*** PAUSED FOR HUMAN: {json.dumps({k: v for k, v in e.items() if k not in ('id', 'created_at')})[:900]}"
    if t in ("thread.created", "thread.done", "sandbox.created", "turn.done", "turn.created", "error", "turn.failed"):
        extra = {k: v for k, v in e.items() if k in ("status", "state", "error", "sandbox_id", "title", "name")}
        if t == "turn.done":
            extra = {"status": (e.get("state") or {}).get("status")}
        return f"{tag}== {t} {json.dumps(extra)[:300]}"
    return None


def _all_events(h: httpx.Client, session_id: str, turn_id: str) -> list[dict]:
    """The events endpoint is paginated; reading only the first page misses the approval pause."""
    events, token = [], None
    while True:
        page = h.get(f"/sessions/{session_id}/turns/{turn_id}/events",
                     params={"page_token": token} if token else {}).json()
        events += page.get("data", [])
        token = (page.get("pagination") or {}).get("next_page_token")
        if not token:
            return events


def follow(session_id: str, timeout_s: int = 1800) -> str:
    """Print events across turns. A turn that ends with required actions (an approval) is a pause, not the
    end: approving starts a new turn, which is followed too. Returns when a turn ends with nothing pending."""
    seen, deadline = set(), time.time() + timeout_s
    with httpx.Client(base_url=TRUEFORGE, timeout=60) as h:
        while time.time() < deadline:
            turns = h.get(f"/sessions/{session_id}/turns").json()
            turns = turns.get("data", turns)
            if not turns:
                time.sleep(2)
                continue
            for turn in turns:
                for e in _all_events(h, session_id, turn["id"]):
                    if e["id"] in seen or e["type"] in QUIET:
                        continue
                    seen.add(e["id"])
                    line = describe(e)
                    if line:
                        print(line, flush=True)
            latest = turns[-1]
            state = latest.get("state") or {}
            if state.get("status") in ("done", "failed", "cancelled") and not state.get("required_actions"):
                return state["status"]
            time.sleep(3)
    return "timeout"

if __name__ == "__main__":
    print("final:", follow(sys.argv[1]))
