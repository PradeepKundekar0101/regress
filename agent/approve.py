"""Answer a pending approval in a TrueForge session from the terminal (fallback for the UI button).

    uv run python -m agent.approve <session_id>                 # show what is waiting
    uv run python -m agent.approve <session_id> allow
    uv run python -m agent.approve <session_id> deny "reason"
"""

import json
import os
import sys

import httpx

TRUEFORGE = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790").rstrip("/") + "/api/v1"


def pending(h: httpx.Client, session_id: str) -> tuple[dict, list[dict]]:
    turn = h.get(f"/sessions/{session_id}/turns").raise_for_status().json()["data"][-1]
    actions = [a for a in (turn.get("state") or {}).get("required_actions") or []
               if a["type"] == "tool.approval_required"]
    return turn, actions


def _all_events(h: httpx.Client, session_id: str, turn_id: str) -> list[dict]:
    events, token = [], None
    while True:
        page = h.get(f"/sessions/{session_id}/turns/{turn_id}/events",
                     params={"page_token": token} if token else {}).raise_for_status().json()
        events += page.get("data", [])
        token = (page.get("pagination") or {}).get("next_page_token")
        if not token:
            return events


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    session_id, decision = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    reason = sys.argv[3] if len(sys.argv) > 3 else None
    with httpx.Client(base_url=TRUEFORGE, timeout=60) as h:
        turn, actions = pending(h, session_id)
        if not actions:
            raise SystemExit(f"nothing is waiting for approval in {session_id} (latest turn is {turn['state']['status']})")
        calls = [(a["thread_id"], c["id"]) for a in actions for c in a["tool_calls"]]
        events = _all_events(h, session_id, turn["id"])
        by_id = {c["id"]: c for e in events if e["type"] == "model.message" for c in e.get("tool_calls") or []}
        for _, call_id in calls:
            fn = by_id.get(call_id, {}).get("function", {})
            print(f"waiting: {fn.get('name', call_id)} {fn.get('arguments', '')}")
        if decision is None:
            return
        if decision not in ("allow", "deny"):
            raise SystemExit("decision must be allow or deny")
        approval = {"status": decision, **({"reason": reason} if decision == "deny" and reason else {})}
        items = [{"type": "user.tool_approval", "thread_id": thread, "tool_call_id": call_id, "approval": approval}
                 for thread, call_id in calls]
        resumed = h.post(f"/sessions/{session_id}/turns", json={"input": items, "stream": False}).raise_for_status().json()
        print(f"{decision}: resumed as turn {resumed['data']['id']}")


if __name__ == "__main__":
    main()
