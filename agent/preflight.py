"""Demo preflight: is everything up, clean and ready? Prints one line per check and exits non-zero on any failure.

    uv run python -m agent.preflight
"""

import json
import os
import sys

import httpx

from regress_mcp.detector import detect
from regress_mcp.store import Store
from target import config
from target.prompts_registry import find_version, production_version

TRUEFORGE = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790").rstrip("/") + "/api/v1"
results: list[tuple[bool, str, str]] = []


def check(name: str):
    def wrap(fn):
        try:
            ok, detail = fn()
        except Exception as exc:  # a preflight reports every failure instead of stopping at the first
            ok, detail = False, f"{type(exc).__name__}: {str(exc)[:160]}"
        results.append((ok, name, detail))
        return fn
    return wrap


@check("bot answers")
def _bot():
    r = httpx.post(f"{config.env('BOT_URL', 'http://localhost:8000')}/reply", timeout=60,
                   json={"question": "What is the daily UPI limit?", "golden_id": "g01", "source": "traffic"})
    body = r.json()
    return r.status_code == 200 and body["meta"]["scores"]["eval_score"] == 1.0, \
        f"HTTP {r.status_code}, prompt v{body.get('meta', {}).get('prompt_version')}, {body.get('meta', {}).get('model')}"


@check("production config is the baseline")
def _config():
    from langfuse import get_client
    lf = get_client()
    live, base = production_version(lf, config.prompt_name()), find_version(lf, config.prompt_name(), "baseline")
    with config.db_connect() as conn:
        model = conn.execute("select model from routes where name = %s", (config.ROUTE_NAME,)).fetchone()[0]
    return live == base and model == config.default_model(), f"prompt v{live} (baseline v{base}), route {model}"


@check("detector baseline is warm and quiet")
def _detector():
    store = Store()
    with config.db_connect() as conn:
        r = detect(conn, mask=store.incident_periods())
    thin = [s["signal"] for s in r["signals"] if not s["baseline_ok"]]
    return not r["alarms"] and not thin, f"alarms {r['alarms'] or 'none'}; thin baselines {thin or 'none'}"


@check("no incident left open")
def _incidents():
    open_now = Store().open_incidents()
    return not open_now, ", ".join(f"{i['id']} ({i['status']})" for i in open_now) or "none open"


@check("TrueForge has the model and agent")
def _trueforge():
    models = [m.get("name") for m in httpx.get(f"{TRUEFORGE}/models", timeout=10).json()["data"]]
    agents = [a["name"] for a in httpx.get(f"{TRUEFORGE}/agents", timeout=10).json()["data"]]
    boot = json.loads((config.ROOT.parent / ".regress" / "bootstrap.json").read_text())
    return boot["model"] in models and "regress" in agents, f"model {boot['model']}, agents {agents}"


@check("TrueForge reaches regress-mcp")
def _connector():
    d = httpx.get(f"{TRUEFORGE}/mcp-servers/regress/tools", timeout=20).json()["data"]
    names = [t["name"] for t in (d["tools"] if isinstance(d, dict) else d)]
    return {"rollback_execute", "route_revert", "run_detector"} <= set(names), f"{len(names)} tools"


@check("TrueForge reaches PostHog MCP (read-only)")
def _posthog():
    d = httpx.get(f"{TRUEFORGE}/mcp-servers/posthog/tools", timeout=30).json()["data"]
    tools = d["tools"] if isinstance(d, dict) else d
    names = {t["name"] for t in tools}
    writable = [t["name"] for t in tools if not (t.get("annotations") or {}).get("readOnlyHint")]
    return "execute-sql" in names and not writable, f"{sorted(names)}; non-read-only: {writable or 'none'}"


@check("Slack bot is in the approval channel")
def _slack():
    token, channel = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_CHANNEL")
    if not (token and channel and os.environ.get("SLACK_APP_TOKEN")):
        return False, "set SLACK_BOT_TOKEN, SLACK_APP_TOKEN and SLACK_CHANNEL"
    r = httpx.post("https://slack.com/api/conversations.info", data={"channel": channel},
                   headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
    if not r.get("ok"):
        return False, r.get("error")
    return bool(r["channel"].get("is_member")), f"#{r['channel']['name']}, member {r['channel'].get('is_member')}"


@check("console connected to Slack")
def _console_slack():
    s = httpx.get("http://localhost:8100/api/slack", timeout=10).json()["status"]
    return s == "connected", s


@check("TrueForge reaches Linear MCP")
def _linear():
    d = httpx.get(f"{TRUEFORGE}/mcp-servers/linear/tools", timeout=30).json()["data"]
    names = [t["name"] for t in (d["tools"] if isinstance(d, dict) else d)]
    return any("issue" in n for n in names), f"{len(names)} tools"


@check("sandbox provider ready")
def _sandbox():
    m = httpx.get(f"{TRUEFORGE}/settings/sandbox-providers", timeout=10).json()["data"]
    minutes = m["manifest"].get("auto_delete_interval_in_minutes")
    return m.get("status") == "ready" and (minutes or 0) <= 120, f"{m.get('status')}, auto-delete {minutes} min"


@check("console up")
def _console():
    r = httpx.get("http://localhost:8100/api/incidents", timeout=20)
    return r.status_code == 200, f"HTTP {r.status_code}"


@check("traffic flowing")
def _traffic():
    with config.db_connect() as conn:
        n = conn.execute("select count(*) from requests where source = 'traffic' and ts > now() - interval '5 minutes'").fetchone()[0]
    return n >= 10, f"{n} requests in the last 5 minutes"


if __name__ == "__main__":
    for ok, name, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:38s} {detail}")
    sys.exit(0 if all(ok for ok, _, _ in results) else 1)
