"""Slack's Approve and Reject buttons, received over Socket Mode so the console needs no public URL.

A click is answered through the same apply_decision the console uses, so Slack can approve only the frozen
proposal. Refusals are told to the clicker alone. Starts from the console's lifespan when SLACK_BOT_TOKEN and
SLACK_APP_TOKEN are set; SLACK_APPROVERS (comma-separated user ids) optionally limits who may decide.
"""

import json
import logging
import os
import time
from collections.abc import Callable

from console.decisions import DecisionRefused
from regress_mcp import slack

Apply = Callable[[str, str, str | None, str], dict]
log = logging.getLogger("regress.slack")
_state: dict = {}


def _actor(user: dict) -> str:
    return f"@{user.get('username') or user.get('name') or user['id']}"


def _tell(channel: str, user_id: str, text: str) -> None:
    try:
        slack.call("chat.postEphemeral", channel=channel, user=user_id, text=text)
    except Exception as exc:  # telling the clicker is best-effort; the decision itself already happened or not
        log.warning("could not reply to %s: %s", user_id, exc)


def _allowed(channel: str, user: dict) -> bool:
    approvers = {u.strip() for u in os.environ.get("SLACK_APPROVERS", "").split(",") if u.strip()}
    if approvers and user["id"] not in approvers:
        _tell(channel, user["id"], "You are not on the approver list for Regress (SLACK_APPROVERS).")
        return False
    return True


def decide_with_retry(apply: Apply, incident_id: str, decision: str, reason: str | None, user: dict, channel: str,
                      *, sleep=time.sleep, wait_s: float = 20.0, every_s: float = 2.0) -> dict | None:
    """Answer the approval, waiting up to wait_s for the agent to pause on it. Tells the clicker if refused."""
    waited, told = 0.0, False
    while True:
        try:
            return apply(incident_id, decision, reason, _actor(user))
        except KeyError:
            _tell(channel, user["id"], f"Unknown incident {incident_id}.")
            return None
        except DecisionRefused as exc:
            if not exc.retryable or waited >= wait_s:
                _tell(channel, user["id"], f"Not {'approved' if decision == 'allow' else 'rejected'}: {exc}")
                return None
            if not told:
                _tell(channel, user["id"], "Waiting for Regress to pause on the approval...")
                told = True
            sleep(every_s)
            waited += every_s


def handle_approve(body: dict, apply: Apply, **retry) -> None:
    user, channel = body["user"], body["channel"]["id"]
    if _allowed(channel, user):
        decide_with_retry(apply, body["actions"][0]["value"], "allow", None, user, channel, **retry)


def handle_reject(body: dict) -> None:
    user, channel = body["user"], body["channel"]["id"]
    if not _allowed(channel, user):
        return
    slack.call("views.open", trigger_id=body["trigger_id"], view={
        "type": "modal", "callback_id": slack.REJECT_VIEW,
        "private_metadata": json.dumps({"incident_id": body["actions"][0]["value"], "channel": channel}),
        "title": {"type": "plain_text", "text": "Reject the fix"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [{"type": "input", "block_id": "reason",
                    "label": {"type": "plain_text", "text": "Why not? Regress records this and stops."},
                    "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                                "max_length": 500}}],
    })


def handle_reject_submit(body: dict, apply: Apply, **retry) -> None:
    meta = json.loads(body["view"]["private_metadata"])
    if not _allowed(meta["channel"], body["user"]):  # the submitter is checked, not only whoever opened the modal
        return
    reason = body["view"]["state"]["values"]["reason"]["value"]["value"]
    decide_with_retry(apply, meta["incident_id"], "deny", reason, body["user"], meta["channel"], **retry)


def start(apply: Apply) -> None:
    bot, app_token = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_APP_TOKEN")
    if not (bot and app_token):
        log.info("Slack approvals off: set SLACK_BOT_TOKEN and SLACK_APP_TOKEN to turn them on")
        return
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    try:
        bolt = App(token=bot)

        @bolt.action(slack.APPROVE)
        def _approve(ack, body):
            ack()
            handle_approve(body, apply)

        @bolt.action(slack.REJECT)
        def _reject(ack, body):
            ack()
            handle_reject(body)

        @bolt.view(slack.REJECT_VIEW)
        def _reject_reason(ack, body):
            ack()
            handle_reject_submit(body, apply)

        handler = SocketModeHandler(bolt, app_token)
        handler.connect()
    except Exception as exc:  # a bad token must not take the console down; the status chip shows it
        _state["error"] = str(exc)
        log.error("Slack approvals failed to start: %s", exc)
        return
    _state["handler"] = handler
    log.info("Slack approvals connected over Socket Mode")


def stop() -> None:
    handler = _state.pop("handler", None)
    if handler:
        handler.close()


def status() -> str:
    handler = _state.get("handler")
    if handler is None:
        return "error" if _state.get("error") else "off"
    return "connected" if handler.client.is_connected() else "disconnected"
