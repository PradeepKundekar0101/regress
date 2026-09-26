"""Slack messages are built from the store, never from agent-written Block Kit."""

import pytest

from regress_mcp import slack


def buttons(blocks):
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


def text_of(blocks):
    return "\n".join(b["text"]["text"] for b in blocks if b["type"] in ("section", "header"))


def test_approval_request_carries_the_frozen_proposal_and_only_the_incident_id(checkpointed, slack_api):
    store, inc = checkpointed
    out = slack.request_approval(store, inc, "Prompt v2 dropped the escalation block.", "https://linear.app/t/REG-1")
    [msg] = slack_api.of("chat.postMessage")
    assert msg["channel"] == "C1"
    assert "Roll back prompt `adopt-support` label `production` from v2 to v1" in text_of(msg["blocks"])
    assert "Prompt v2 dropped the escalation block." in text_of(msg["blocks"])
    assert [(b["action_id"], b["value"]) for b in buttons(msg["blocks"])] == [
        (slack.APPROVE, inc), (slack.REJECT, inc)]
    assert "https://linear.app/t/REG-1" in str(msg["blocks"])
    n = store.notification(inc)
    assert (n["channel"], n["ts"], out["updated"]) == ("C1", out["ts"], False)


def test_asking_again_updates_the_same_message(checkpointed, slack_api):
    store, inc = checkpointed
    first = slack.request_approval(store, inc, "first", None)
    again = slack.request_approval(store, inc, "second", None)
    assert len(slack_api.of("chat.postMessage")) == 1
    [upd] = slack_api.of("chat.update")
    assert upd["ts"] == first["ts"] and again["updated"] is True
    assert "second" in text_of(upd["blocks"])


def test_asking_again_after_a_decision_says_so_under_live_buttons(checkpointed, slack_api):
    store, inc = checkpointed
    slack.request_approval(store, inc, "first", None)
    assert "Previously decided" not in str(slack_api.of("chat.postMessage")[0]["blocks"])
    store.claim_decision(inc, "@ana", "call_0")  # answered, then the agent resumed and re-asked
    slack.request_approval(store, inc, "second", None)
    [upd] = slack_api.of("chat.update")
    assert [b["action_id"] for b in buttons(upd["blocks"])] == [slack.APPROVE, slack.REJECT]
    last = upd["blocks"][-1]
    assert last["type"] == "context"
    assert last["elements"][0]["text"] == "Previously decided by @ana; Regress is asking again."


def test_refuses_unless_checkpointed(checkpointed, slack_api):
    store, inc = checkpointed
    store.transition(inc, "denied", "no", [])
    with pytest.raises(ValueError, match="checkpointed"):
        slack.request_approval(store, inc, "summary", None)
    assert slack_api.calls == []


def test_refuses_unrendered_placeholders(checkpointed, slack_api):
    store, inc = checkpointed
    with pytest.raises(ValueError, match="rendered"):
        slack.request_approval(store, inc, "eval fell to {{ev_1}}", None)
    with pytest.raises(ValueError, match="rendered"):
        slack.post_update(store, inc, "recovered to {{ev_2}}")


def test_updates_thread_under_the_approval(checkpointed, slack_api):
    store, inc = checkpointed
    asked = slack.request_approval(store, inc, "summary", None)
    out = slack.post_update(store, inc, "Verified: eval 0.99 vs 0.52 before.")
    reply = slack_api.of("chat.postMessage")[-1]
    assert reply["thread_ts"] == asked["ts"] and out["thread_ts"] == asked["ts"]


def test_update_without_a_message_starts_one_then_threads(checkpointed, slack_api):
    store, inc = checkpointed
    first = slack.post_update(store, inc, "NOT_LOCALIZED: nothing changed in the window.")
    second = slack.post_update(store, inc, "Filed in Linear.")
    top, reply = slack_api.of("chat.postMessage")
    assert "thread_ts" not in top and inc in top["text"]
    assert reply["thread_ts"] == first["ts"] and second["thread_ts"] == first["ts"]


def test_decided_message_drops_the_buttons_and_names_the_decider(checkpointed, slack_api):
    store, inc = checkpointed
    slack.request_approval(store, inc, "summary", None)
    assert slack.mark_decided(store, inc, "deny", "@ana", "we chose the bigger model") is True
    [upd] = slack_api.of("chat.update")
    assert buttons(upd["blocks"]) == []
    assert "Rejected* by @ana" in text_of(upd["blocks"]) and "we chose the bigger model" in text_of(upd["blocks"])


def test_mark_decided_without_a_message_is_a_no_op(checkpointed, slack_api):
    store, inc = checkpointed
    assert slack.mark_decided(store, inc, "allow", "console") is False
    assert slack_api.calls == []


def test_not_configured_is_a_clear_refusal(checkpointed):
    store, inc = checkpointed
    with pytest.raises(ValueError, match="not configured"):
        slack.request_approval(store, inc, "summary", None)
