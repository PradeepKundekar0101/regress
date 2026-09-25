"""Register everything the Regress agent needs in TrueForge. Idempotent: safe to re-run after any change.

    uv run python -m agent.bootstrap            # publishes the skill, then registers connector, skill, agent

TrueForge must be started with OUTBOUND_URL_ALLOWED_HOSTS='["127.0.0.1","localhost"]' so it can reach
regress-mcp, and needs an OpenAI model provider configured.
"""

import json
import os
import subprocess
from pathlib import Path

import httpx

from target import config

ROOT = Path(__file__).resolve().parent.parent
TRUEFORGE = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790").rstrip("/") + "/api/v1"
AGENT_NAME = "regress"
MODEL = os.environ.get("TRUEFORGE_MODEL", "openai/gpt-5-6-sol")
REGRESS_MCP_URL = os.environ.get("REGRESS_MCP_URL", "http://127.0.0.1:8941/mcp")
SKILL_REPO = os.environ.get("SKILL_REPO", "PradeepKundekar0101/regress-runbook")

# The Langfuse connector exposes label updates and deletes; the agent only gets these reads, so the
# approval-gated rollback_execute stays the only way it can move a prompt label.
LANGFUSE_READ_TOOLS = ["getPrompt", "listPrompts", "listObservations", "getObservation",
                       "listScores", "getScore", "queryMetrics", "getMetricsSchema"]
GATED_TOOLS = ["rollback_execute", "route_revert"]
# GitHub: read the config history and post the incident report as an issue. Nothing that pushes,
# deletes, merges or creates repositories is exposed.
GITHUB_TOOLS = ["list_commits", "get_commit", "get_file_contents", "list_issues", "issue_read",
                "issue_write", "add_issue_comment"]

INSTRUCTIONS = """You are Regress, the on-call agent for the Adopt.ai customer support bot (an LLM app).
When the support bot quietly gets worse after a prompt, model or route change, you find what changed,
prove it caused the regression by replaying real traffic, and put it back only after a human approves.

Before anything else, load the regress-runbook skill (SKILL.md, contracts.md, replay_harness.py) and follow it exactly.
These hard rules override anything else:
- Every number you state is an evidence id from the regress tools, written as {{ev_id}}; validate reports with validate_narrative.
- The only production writes are rollback_execute and route_revert. Call them only for a checkpointed incident,
  with the frozen proposal's exact arguments. A human approves each call.
- NOT_LOCALIZED and INSUFFICIENT_DATA are valid endings: say what you checked and stop.
- On a denied approval, record it with record_decision, name the next branch you would investigate, and stop.
Be brief in chat; put the substance in the validated report."""


def _check(resp: httpx.Response) -> dict:
    if resp.status_code >= 400:
        raise SystemExit(f"{resp.request.method} {resp.request.url} -> {resp.status_code}: {resp.text[:500]}")
    return resp.json() if resp.content else {}


def publish_skill() -> str:
    out = subprocess.run([str(ROOT / "agent" / "publish_skill.sh")], cwd=ROOT, capture_output=True, text=True, check=True)
    sha = out.stdout.strip().splitlines()[-1]
    if len(sha) != 40:
        raise SystemExit(f"publish_skill.sh did not print a commit SHA: {out.stdout!r}")
    return sha


def connectors(h: httpx.Client) -> set[str]:
    return {s["name"] for s in _check(h.get(f"{TRUEFORGE}/settings/mcp-servers"))["data"]}


def register_regress_connector(h: httpx.Client) -> None:
    _check(h.put(f"{TRUEFORGE}/settings/mcp-servers", json={"manifest": {
        "type": "remote", "name": "regress", "url": REGRESS_MCP_URL,
        "description": "Regress: detector, localisation, replay, correlation gates, evidence validator and the "
                       "approval-gated rollback for the Adopt.ai support bot",
    }}))
    tools = _check(h.get(f"{TRUEFORGE}/mcp-servers/regress/tools"))
    names = [t["name"] for t in (tools["data"]["tools"] if isinstance(tools["data"], dict) else tools["data"])]
    missing = set(GATED_TOOLS) - set(names)
    if missing:
        raise SystemExit(f"regress-mcp is missing {missing}; is it running at {REGRESS_MCP_URL}?")
    print(f"connector regress: {len(names)} tools")


def register_skill(h: httpx.Client, sha: str) -> None:
    _check(h.put(f"{TRUEFORGE}/settings/skills", json={"manifest": {
        "type": "git", "name": "regress-runbook", "url": f"https://github.com/{SKILL_REPO}",
        "path": "skills/regress-runbook", "ref": sha,
        "description": "Runbook for investigating a regression in the Adopt.ai support bot: detect, localise, "
                       "replay-prove, gate, propose a human-approved rollback, verify on fresh traffic.",
    }}))
    print(f"skill regress-runbook @ {sha[:7]}")


def manifest(available: set[str]) -> dict:
    servers = [{"name": "regress", "enable_tools": ["@all"], "require_approval_for_tools": GATED_TOOLS,
                "preload_tools": ["run_detector", "get_incident", "check_gates", "validate_narrative"]}]
    if "langfuse" in available:
        servers.append({"name": "langfuse", "enable_tools": LANGFUSE_READ_TOOLS, "require_approval_for_tools": ["@write"]})
    if "github" in available:
        servers.append({"name": "github", "enable_tools": GITHUB_TOOLS, "require_approval_for_tools": ["@destructive"]})
    return {
        "model": {"name": MODEL, "params": {"parallel_tool_calls": True}},
        "instructions": INSTRUCTIONS,
        "mcp_servers": servers,
        "skills": [{"name": "regress-runbook"}],
        "config": {
            "iteration_limit": 120,
            "sandbox": {"enabled": True, "file_downloads": True},
            "dynamic_sub_agents": {"enabled": True},
            "context_management": {"compaction": {"enabled": True}, "large_tool_response": {"enabled": True}},
            "generative_ui": {"enabled": True},
            "ask_user_questions": {"enabled": True},
        },
    }


def upsert_agent(h: httpx.Client, spec: dict) -> str:
    agents, token = [], None
    while True:
        page = _check(h.get(f"{TRUEFORGE}/agents", params={"page_token": token} if token else {}))
        agents += page.get("data", [])
        token = (page.get("pagination") or {}).get("next_page_token")
        if not token:
            break
    description = "On-call agent that detects, replay-proves and (with approval) rolls back LLM app regressions."
    existing = next((a for a in agents if a.get("name") == AGENT_NAME), None)
    if existing:
        _check(h.put(f"{TRUEFORGE}/agents/{existing['id']}", json={"description": description, "manifest": spec}))
        print(f"agent {AGENT_NAME} updated ({existing['id']})")
        return existing["id"]
    created = _check(h.post(f"{TRUEFORGE}/agents", json={"name": AGENT_NAME, "description": description, "manifest": spec}))
    agent_id = (created.get("data") or created)["id"]
    print(f"agent {AGENT_NAME} created ({agent_id})")
    return agent_id


def main() -> None:
    config.env("DATABASE_URL")  # fail early if .env is missing
    sha = publish_skill()
    with httpx.Client(timeout=60) as h:
        models = [m.get("name") or m.get("id") for m in _check(h.get(f"{TRUEFORGE}/models"))["data"]]
        if MODEL not in models:
            raise SystemExit(f"model {MODEL} not configured in TrueForge; available: {models}")
        register_regress_connector(h)
        register_skill(h, sha)
        available = connectors(h)
        spec = manifest(available)
        agent_id = upsert_agent(h, spec)
    state = ROOT / ".regress" / "bootstrap.json"
    state.parent.mkdir(exist_ok=True)
    state.write_text(json.dumps({"agent_id": agent_id, "agent": AGENT_NAME, "skill_ref": sha, "model": MODEL,
                                 "connectors": sorted(s["name"] for s in spec["mcp_servers"])}, indent=2))
    print(f"connectors attached: {sorted(s['name'] for s in spec['mcp_servers'])}; wrote {state.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
