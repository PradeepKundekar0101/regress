"""Slack messages for incidents: the approval request with its buttons, thread updates and the decided state.

The buttons are built here from the incident's frozen proposal and carry only the incident id, so a click
can approve only what the store froze. The agent supplies prose, never Block Kit.
"""

import logging
import os
from datetime import datetime, timezone

import httpx

from regress_mcp.store import Store

API = "https://slack.com/api/"
APPROVE, REJECT, REJECT_VIEW = "regress_approve", "regress_reject", "regress_reject_reason"
transport: httpx.BaseTransport | None = None  # tests swap in a MockTransport
TRUEFORGE_ACTOR = "a human in TrueForge"
log = logging.getLogger("regress.slack")


class SlackError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_CHANNEL"))


def call(method: str, **payload) -> dict:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise SlackError("SLACK_BOT_TOKEN is not set")
    with httpx.Client(base_url=API, timeout=15, transport=transport,
                      headers={"Authorization": f"Bearer {token}"}) as h:
        body = h.post(method, json=payload).raise_for_status().json()
    if not body.get("ok"):
        raise SlackError(f"slack {method}: {body.get('error')}")
    return body


def approvers() -> list[str]:
    """Slack user ids allowed to decide (SLACK_APPROVERS, comma-separated); empty means anyone in the channel."""
    return [u.strip() for u in os.environ.get("SLACK_APPROVERS", "").split(",") if u.strip()]


def mention() -> str:
    """Who a new incident message pings: the approvers, else everyone active in the channel.

    Slack only notifies for mentions, so a message without one lands silently.
    """
    return " ".join(f"<@{u}>" for u in approvers()) or "<!here>"


def _require(text: str) -> None:
    if not configured():
        raise ValueError("Slack is not configured (SLACK_BOT_TOKEN, SLACK_CHANNEL); the console can still approve")
    if "{{" in text:
        raise ValueError("text still has {{ev_id}} placeholders; post the validator's rendered text")


def proposal_text(p: dict) -> str:
    if p["action"] == "rollback_execute":
        change = f"Roll back prompt `{p['prompt']}` label `{p['label']}` from v{p['from_version']} to v{p['to_version']}"
    else:
        change = f"Point route `{p['route']}` from `{p['from_model']}` back to `{p['to_model']}`"
    lines = [f"*Proposed:* {change}"]
    blast = p.get("blast_radius") or {}
    if "affected_requests_in_window" in blast:
        lines.append(f"*Blast radius:* {blast['affected_requests_in_window']} of {blast['requests_in_window']} "
                     f"requests in the window, {blast.get('scope', 'one change')}")
    if p.get("undo"):
        lines.append(f"*Undo:* {p['undo']}")
    return "\n".join(lines)


def approval_blocks(incident: dict, summary: str, linear_url: str | None, decided: str | None = None) -> list[dict]:
    console = f"{os.environ.get('CONSOLE_URL', 'http://localhost:8100').rstrip('/')}/#incidents/{incident['id']}"
    links = [f"<{console}|Open in console>"] + ([f"<{linear_url}|Linear issue>"] if linear_url else [])
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"Regress {incident['id']}: {incident['verdict'] or 'needs a decision'}"[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary[:2900] or " "}},
        {"type": "section", "text": {"type": "mrkdwn", "text": proposal_text(incident["proposal"])}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": " - ".join(links)}]},
    ]
    if decided:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": decided}})
        return blocks
    blocks.append({"type": "actions", "block_id": "regress_decision", "elements": [
        {"type": "button", "action_id": APPROVE, "style": "primary", "value": incident["id"],
         "text": {"type": "plain_text", "text": "Approve"},
         "confirm": {"title": {"type": "plain_text", "text": "Change production?"},
                     "text": {"type": "mrkdwn", "text": "Regress applies the proposed change and verifies it on fresh traffic."},
                     "confirm": {"type": "plain_text", "text": "Approve"},
                     "deny": {"type": "plain_text", "text": "Cancel"}}},
        {"type": "button", "action_id": REJECT, "style": "danger", "value": incident["id"],
         "text": {"type": "plain_text", "text": "Reject"}},
    ]})
    return blocks


def request_approval(store: Store, incident_id: str, summary: str, linear_url: str | None = None) -> dict:
    incident = store.incident(incident_id)
    if incident["status"] != "checkpointed" or not incident["proposal"]:
        raise ValueError(f"{incident_id} is {incident['status']}; only a checkpointed incident can ask for approval")
    _require(summary)
    blocks = approval_blocks(incident, summary, linear_url)
    text = f"Regress {incident_id} needs a decision"
    existing = store.notification(incident_id)
    if existing and existing["decided_by"]:
        # A re-ask after a resume needs a fresh decision (the lock is per pending call); say it was decided before.
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                       f"Previously decided by {existing['decided_by']}; Regress is asking again."}]})
    if existing and existing["ts"]:
        call("chat.update", channel=existing["channel"], ts=existing["ts"], text=text, blocks=blocks)
        channel, ts, updated = existing["channel"], existing["ts"], True
        if existing["decided_by"]:  # an edit does not notify, so ping in the thread
            call("chat.postMessage", channel=channel, thread_ts=ts, text=f"{mention()} {text} again")
    else:
        ping = [{"type": "context", "elements": [{"type": "mrkdwn", "text": mention()}]}]
        posted = call("chat.postMessage", channel=os.environ["SLACK_CHANNEL"], text=f"{mention()} {text}",
                      blocks=ping + blocks)
        channel, ts, updated = posted["channel"], posted["ts"], False
    store.save_notification(incident_id, channel, ts, summary=summary, linear_url=linear_url)
    return {"channel": channel, "ts": ts, "updated": updated}


def post_update(store: Store, incident_id: str, text: str) -> dict:
    store.incident(incident_id)  # KeyError for an unknown incident
    _require(text)
    existing = store.notification(incident_id)
    if existing and existing["ts"]:
        posted = call("chat.postMessage", channel=existing["channel"], thread_ts=existing["ts"], text=text)
        return {"channel": existing["channel"], "ts": posted["ts"], "thread_ts": existing["ts"]}
    posted = call("chat.postMessage", channel=os.environ["SLACK_CHANNEL"], text=f"{mention()} *Regress {incident_id}*\n{text}")
    store.save_notification(incident_id, posted["channel"], posted["ts"])
    return {"channel": posted["channel"], "ts": posted["ts"], "thread_ts": None}


def mark_decided(store: Store, incident_id: str, decision: str, actor: str, reason: str | None = None) -> bool:
    """Replace the buttons with who decided and when. False when there is no Slack message to update."""
    n = store.notification(incident_id)
    if not (n and n["ts"] and os.environ.get("SLACK_BOT_TOKEN")):
        return False
    at = datetime.now(timezone.utc).strftime("%H:%M UTC")
    if decision == "allow":
        line = f":white_check_mark: *Approved* by {actor} at {at}"
    else:
        line = f":x: *Rejected* by {actor} at {at}" + (f": {reason}" if reason else "")
    incident = store.incident(incident_id)
    call("chat.update", channel=n["channel"], ts=n["ts"], text=line,
         blocks=approval_blocks(incident, n["summary"] or "", n["linear_url"], decided=line))
    return True


def settle_outside_decision(store: Store, incident_id: str, decision: str, reason: str | None = None) -> bool:
    """Show a decision made outside Slack and the console on the incident's Slack message.

    TrueForge's own Allow/Deny (or agent/approve.py) never passes the console's decision path, so the message
    would keep live buttons. Best-effort and never raises: the gated action or the denial has already happened.
    """
    n = store.notification(incident_id)
    if not n or not n["ts"] or n["decided_by"]:
        return False
    try:
        return mark_decided(store, incident_id, decision, TRUEFORGE_ACTOR, reason)
    except Exception as exc:  # a stale Slack message must never fail a production action
        log.warning("could not show the TrueForge decision for %s in Slack: %s", incident_id, exc)
        return False
