"""The Linear connector exposes an explicit allowlist of issue tools, never whatever the server happens to offer."""

import httpx
import pytest

from agent import bootstrap


def linear_server(names: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"data": {"tools": [{"name": n} for n in names]}})
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def linear_env(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.setenv("LINEAR_TEAM", "REG")


def test_only_allowlisted_linear_tools_are_enabled(capsys):
    offered = ["create_issue", "update_issue", "get_issue", "list_teams", "create_comment",
               "delete_issue", "archive_project", "create_project", "update_team", "create_document"]
    with linear_server(offered) as h:
        tools = bootstrap.register_linear_connector(h)
    assert tools == ["create_comment", "create_issue", "get_issue", "list_teams", "update_issue"]
    out = capsys.readouterr().out
    assert "save_issue" in out  # allowlisted names the server does not offer are reported


def test_newer_save_naming_is_accepted():
    with linear_server(["save_issue", "save_comment", "list_issue_statuses"]) as h:
        assert bootstrap.register_linear_connector(h) == ["list_issue_statuses", "save_comment", "save_issue"]


def test_a_server_that_cannot_file_an_issue_stops_bootstrap():
    with linear_server(["list_issues", "get_issue"]) as h, pytest.raises(SystemExit):
        bootstrap.register_linear_connector(h)


def test_linear_writes_are_not_approval_gated():
    spec = bootstrap.manifest({"linear"}, ["create_issue", "update_issue"])
    [linear] = [s for s in spec["mcp_servers"] if s["name"] == "linear"]
    assert linear["enable_tools"] == ["create_issue", "update_issue"]
    assert linear["require_approval_for_tools"] == []
