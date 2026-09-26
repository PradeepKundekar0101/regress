"""Shared test setup. target.config loads the real .env on import, so Slack settings are cleared for every test."""

import pytest

SLACK_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL", "SLACK_APPROVERS")


@pytest.fixture(autouse=True)
def _no_real_slack(monkeypatch):
    for name in SLACK_ENV:
        monkeypatch.delenv(name, raising=False)
