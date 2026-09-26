"""Shared test setup. target.config loads the real .env on import, so Slack settings are cleared for every test."""

import json

import httpx
import pytest

from regress_mcp import slack
from regress_mcp.store import Store

SLACK_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL", "SLACK_APPROVERS")

PROPOSAL = {"action": "rollback_execute", "prompt": "adopt-support", "label": "production",
            "from_version": 2, "to_version": 1, "undo": "flip the production label back to v2",
            "blast_radius": {"affected_requests_in_window": 41, "requests_in_window": 41, "scope": "one prompt label"}}


@pytest.fixture(autouse=True)
def _no_real_slack(monkeypatch):
    for name in SLACK_ENV:
        monkeypatch.delenv(name, raising=False)


class FakeSlack:
    """Records every Web API call and answers ok with a fresh ts."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content) if request.content else {}
        self.calls.append((method, payload))
        return httpx.Response(200, json={"ok": True, "channel": payload.get("channel", "C1"),
                                         "ts": f"100.{len(self.calls)}"})

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def slack_api(monkeypatch):
    fake = FakeSlack()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C1")
    monkeypatch.setattr(slack, "transport", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def checkpointed(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    inc = store.open_incident("eval_score", [])
    for status in ["planned", "replayed"]:
        store.transition(inc, status, "", [])
    store.transition(inc, "checkpointed", "[]", [], proposal=PROPOSAL, verdict="LOCALIZED_PROMPT")
    return store, inc
