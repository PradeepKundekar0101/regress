"""Slack clicks go through the same decision path as the console; refusals are told to the clicker only."""

import json

from console import slack as bridge
from console.decisions import DecisionRefused
from regress_mcp import slack


def click(action: str, incident: str = "inc_1", user: str = "U1") -> dict:
    return {"user": {"id": user, "username": "ana"}, "channel": {"id": "C1"}, "trigger_id": "trig",
            "actions": [{"action_id": action, "value": incident}]}


class Apply:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, incident_id, decision, reason, actor):
        self.calls.append((incident_id, decision, reason, actor))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def no_sleep(_):
    pass


def test_approve_answers_as_the_slack_user(slack_api):
    apply = Apply({"ok": True})
    bridge.handle_approve(click(slack.APPROVE), apply, sleep=no_sleep)
    assert apply.calls == [("inc_1", "allow", None, "@ana")]
    assert slack_api.of("chat.postEphemeral") == []


def test_click_before_the_pause_waits_then_answers(slack_api):
    apply = Apply(DecisionRefused("not paused", retryable=True), {"ok": True})
    slept = []
    bridge.handle_approve(click(slack.APPROVE), apply, sleep=slept.append)
    assert len(apply.calls) == 2 and slept == [2.0]
    [told] = slack_api.of("chat.postEphemeral")
    assert "Waiting for Regress" in told["text"] and told["user"] == "U1"


def test_gives_up_after_the_wait_and_says_why(slack_api):
    apply = Apply(*[DecisionRefused("Regress has not paused on the approval yet", retryable=True)] * 5)
    bridge.handle_approve(click(slack.APPROVE), apply, sleep=no_sleep, wait_s=4, every_s=2)
    assert len(apply.calls) == 3
    assert "Not approved: Regress has not paused" in slack_api.of("chat.postEphemeral")[-1]["text"]


def test_refusal_is_told_at_once(slack_api):
    apply = Apply(DecisionRefused("inc_1 was already decided by console"))
    bridge.handle_approve(click(slack.APPROVE), apply, sleep=no_sleep)
    assert len(apply.calls) == 1
    assert "already decided by console" in slack_api.of("chat.postEphemeral")[0]["text"]


def test_reject_asks_for_a_reason_first(slack_api):
    bridge.handle_reject(click(slack.REJECT))
    [opened] = slack_api.of("views.open")
    assert opened["trigger_id"] == "trig" and opened["view"]["callback_id"] == slack.REJECT_VIEW
    assert json.loads(opened["view"]["private_metadata"]) == {"incident_id": "inc_1", "channel": "C1"}


def test_reason_submission_denies_with_it(slack_api):
    apply = Apply({"ok": True})
    body = {"user": {"id": "U1", "username": "ana"}, "view": {
        "private_metadata": json.dumps({"incident_id": "inc_1", "channel": "C1"}),
        "state": {"values": {"reason": {"value": {"value": "we chose the bigger model"}}}}}}
    bridge.handle_reject_submit(body, apply, sleep=no_sleep)
    assert apply.calls == [("inc_1", "deny", "we chose the bigger model", "@ana")]


def test_only_listed_approvers_may_decide(slack_api, monkeypatch):
    monkeypatch.setenv("SLACK_APPROVERS", "U9, U8")
    apply = Apply({"ok": True})
    bridge.handle_approve(click(slack.APPROVE, user="U1"), apply, sleep=no_sleep)
    bridge.handle_reject(click(slack.REJECT, user="U1"))
    assert apply.calls == [] and slack_api.of("views.open") == []
    assert all("not on the approver list" in t["text"] for t in slack_api.of("chat.postEphemeral"))


def test_status_is_off_until_started():
    assert bridge.status() == "off"


def test_api_slack_reports_off_with_no_tokens():
    from fastapi.testclient import TestClient

    from console.app import app

    with TestClient(app) as client:
        assert client.get("/api/slack").json() == {"status": "off"}
