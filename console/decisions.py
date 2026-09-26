"""Answering the agent's one pending approval, from the console or from Slack, under the same checks.

A decision is refused unless the incident is checkpointed, exactly one gated call is pending and its
arguments equal the frozen proposal. The first decision takes a lock in the store, so a second click from
either surface is refused with who decided.
"""

import logging
from collections.abc import Callable

import httpx

from console import trueforge
from regress_mcp import slack
from regress_mcp.store import Store

GATED_TOOLS = {"rollback_execute", "route_revert"}
log = logging.getLogger("regress.decisions")


class DecisionRefused(Exception):
    """Not answered. `retryable` means the agent has not paused on the call yet, so asking again soon may work."""

    def __init__(self, detail: str, retryable: bool = False):
        super().__init__(detail)
        self.retryable = retryable


def _proposal_args(proposal: dict) -> tuple[str, dict]:
    keys = {"rollback_execute": ("prompt", "label", "from_version", "to_version"),
            "route_revert": ("route", "from_model", "to_model")}[proposal["action"]]
    return proposal["action"], {k: proposal[k] for k in keys}


def apply_decision(store: Store, tf_client: Callable[[], httpx.Client], incident_id: str, decision: str,
                   reason: str | None, actor: str) -> dict:
    inc = store.incident(incident_id)
    held = (store.notification(incident_id) or {}).get("decided_by")
    if held:
        raise DecisionRefused(f"{incident_id} was already decided by {held}")
    if inc["status"] != "checkpointed" or not inc["proposal"]:
        raise DecisionRefused(f"{incident_id} is {inc['status']}; only a checkpointed incident awaits a decision")
    with tf_client() as h:
        sid = trueforge.session_for(incident_id, h)
        if sid is None:
            raise DecisionRefused("no TrueForge session is linked to this incident", retryable=True)
        view = trueforge.session_view(h, sid)
        gated = [p for p in view["pending"] if p["tool"] in GATED_TOOLS]
        if not gated:
            raise DecisionRefused("Regress has not paused on the approval yet", retryable=True)
        if len(gated) > 1:
            raise DecisionRefused(f"expected exactly one pending gated call, found {len(gated)}")
        pending = gated[0]
        tool, expected = _proposal_args(inc["proposal"])
        actual = {k: pending["arguments"].get(k) for k in expected}
        if pending["tool"] != tool or actual != expected or pending["arguments"].get("incident_id") != incident_id:
            raise DecisionRefused(f"the pending call does not match the frozen proposal: {pending['tool']} {actual}")
        held = store.claim_decision(incident_id, actor)
        if held:
            raise DecisionRefused(f"{incident_id} was already decided by {held}")
        note = reason if actor == "console" or decision == "allow" else f"{reason or 'no reason given'} (rejected by {actor} in Slack)"
        try:
            turn_id = trueforge.answer(h, sid, pending, decision, note)
        except Exception:
            store.release_decision(incident_id)
            raise
    try:
        slack.mark_decided(store, incident_id, decision, actor, reason)
    except (slack.SlackError, httpx.HTTPError) as exc:  # the decision is made; a stale Slack message is cosmetic
        log.warning("could not update the Slack message for %s: %s", incident_id, exc)
    return {"ok": True, "decision": decision, "turn_id": turn_id, "session_url": view["url"]}
