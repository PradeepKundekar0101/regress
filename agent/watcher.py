"""Watch the bot and hand new incidents to the Regress agent.

Every POLL_SECONDS it calls regress-mcp's run_detector. When the detector opens a new incident, it starts a
TrueForge session for the `regress` agent with the incident id, and prints the link where the agent's work
and the approval card appear.

    uv run python -m agent.watcher            # poll forever
    uv run python -m agent.watcher --once     # one detector run
"""

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

import anyio
import httpx
from mcp.client import Client

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".regress" / "watcher.json"
TRUEFORGE = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790").rstrip("/")
REGRESS_MCP_URL = os.environ.get("REGRESS_MCP_URL", "http://127.0.0.1:8941/mcp")
POLL_SECONDS = int(os.environ.get("WATCH_INTERVAL_SECONDS", "60"))


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {"sessions": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


async def detect() -> dict:
    async with Client(REGRESS_MCP_URL) as c:
        result = await c.call_tool("run_detector", {"window_minutes": 5, "open_incident": True})
    if result.is_error:
        raise RuntimeError(result.content[0].text)
    return json.loads(result.content[0].text)


def start_session(incident_id: str, det: dict) -> str:
    moved = [f"{s['signal']} {s['current']:.4g} vs baseline {s['baseline_median']:.4g} (z {s['z']})"
             for s in det["signals"] if s["alarm"]]
    message = (f"Detector alarm: incident {incident_id} is open on the Adopt.ai support bot.\n"
               f"Window {det['window']['from']} to {det['window']['to']}.\nAlarming signals:\n- " + "\n- ".join(moved) +
               "\nInvestigate it with the regress-runbook.")
    with httpx.Client(base_url=f"{TRUEFORGE}/api/v1", timeout=60) as h:
        session = h.post("/sessions", json={"agent": {"name": "regress"},
                                            "metadata": {"incident_id": incident_id}}).raise_for_status().json()
        sid = (session.get("data") or session)["id"]
        h.post(f"/sessions/{sid}/turns", json={"input": [{"type": "user.message", "content": message}],
                                               "stream": False}).raise_for_status()
    return sid


def tick(state: dict) -> None:
    det = anyio.run(detect)
    incident_id = det.get("incident_id")
    if not det["alarms"]:
        judged = [x for x in det["signals"] if x.get("baseline_ok", True)]
        if not det["signals"] or len(judged) < len(det["signals"]):
            log(f"NO BASELINE: only {len(judged)} of 9 signals have 30 min of clean history; faults cannot be detected yet")
        else:
            log(f"quiet ({len(judged)} signals in band)")
        return
    if det.get("suppressed_by"):
        log(f"alarms {det['alarms']}: {det['note']}")
        return
    if incident_id in state["sessions"]:
        log(f"alarms {det['alarms']} belong to {incident_id}, already handed to session {state['sessions'][incident_id]}")
        return
    sid = start_session(incident_id, det)
    state["sessions"][incident_id] = sid
    save_state(state)
    log(f"ALARM {det['alarms']} -> opened {incident_id}; Regress is on it: {TRUEFORGE}/sessions/{sid}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    state = load_state()
    while True:
        try:
            tick(state)
        except Exception as exc:  # keep watching through transient failures, but say so
            log(f"detector run failed: {exc!r}")
        if args.once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
