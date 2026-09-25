"""The console's only write: answering a pending TrueForge approval, guarded by the incident state."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from console import app as console_app
from console import trueforge
from regress_mcp.store import Store

PROPOSAL = {"action": "rollback_execute", "prompt": "adopt-support", "label": "production",
            "from_version": 2, "to_version": 1, "blast_radius": {}, "undo": "flip back"}


def pending_call(args: dict, tool: str = "rollback_execute") -> dict:
    return {"id": "call_1", "function": {"name": "call_tool", "arguments": json.dumps(
        {"mcp_server": "regress", "tool_name": tool, "input": args})}}


class FakeTrueForge:
    def __init__(self, call: dict | None):
        self.call, self.posted = call, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/turns") and request.method == "GET":
            required = [{"type": "tool.approval_required", "thread_id": "main",
                         "tool_calls": [{"id": "call_1"}]}] if self.call else []
            return httpx.Response(200, json={"data": [{"id": "turn_1", "state": {"status": "done", "required_actions": required}}]})
        if path.endswith("/events"):
            return httpx.Response(200, json={"data": [{"type": "model.message", "tool_calls": [self.call]}] if self.call else []})
        if path.endswith("/turns") and request.method == "POST":
            self.posted.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"id": "turn_2"}})
        return httpx.Response(404, json={})


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite")
    inc = store.open_incident("eval_score", [])
    for status in ["planned", "replayed"]:
        store.transition(inc, status, "", [])
    store.transition(inc, "checkpointed", "[]", [], proposal=PROPOSAL, verdict="LOCALIZED_PROMPT")
    monkeypatch.setattr(console_app, "state", {"store": store})
    monkeypatch.setattr(trueforge, "session_for", lambda incident_id, h: "sess_1")

    def use(fake):
        console_app.state["tf_transport"] = httpx.MockTransport(fake)
        return TestClient(console_app.app)
    return store, inc, use


def good_args(inc):
    return {"incident_id": inc, "prompt": "adopt-support", "label": "production", "from_version": 2, "to_version": 1}


def test_allow_answers_the_pending_approval(env):
    store, inc, use = env
    fake = FakeTrueForge(pending_call(good_args(inc)))
    resp = use(fake).post(f"/api/incidents/{inc}/decision", json={"decision": "allow"})
    assert resp.status_code == 200, resp.text
    item = fake.posted[0]["input"][0]
    assert item == {"type": "user.tool_approval", "thread_id": "main", "tool_call_id": "call_1",
                    "approval": {"status": "allow"}}


def test_deny_carries_the_reason(env):
    store, inc, use = env
    fake = FakeTrueForge(pending_call(good_args(inc)))
    use(fake).post(f"/api/incidents/{inc}/decision", json={"decision": "deny", "reason": "not during peak"})
    assert fake.posted[0]["input"][0]["approval"] == {"status": "deny", "reason": "not during peak"}


def test_refuses_when_pending_call_differs_from_proposal(env):
    store, inc, use = env
    fake = FakeTrueForge(pending_call({**good_args(inc), "to_version": 7}))
    resp = use(fake).post(f"/api/incidents/{inc}/decision", json={"decision": "allow"})
    assert resp.status_code == 409 and "does not match the frozen proposal" in resp.text
    assert fake.posted == []


def test_refuses_when_nothing_is_pending(env):
    store, inc, use = env
    fake = FakeTrueForge(None)
    resp = use(fake).post(f"/api/incidents/{inc}/decision", json={"decision": "allow"})
    assert resp.status_code == 409 and fake.posted == []


def test_refuses_unless_checkpointed(env):
    store, inc, use = env
    store.transition(inc, "denied", "already decided", [])
    fake = FakeTrueForge(pending_call(good_args(inc)))
    resp = use(fake).post(f"/api/incidents/{inc}/decision", json={"decision": "allow"})
    assert resp.status_code == 409 and "denied" in resp.text and fake.posted == []


def test_rejects_unknown_decisions(env):
    store, inc, use = env
    resp = use(FakeTrueForge(None)).post(f"/api/incidents/{inc}/decision", json={"decision": "approve-everything"})
    assert resp.status_code == 422
