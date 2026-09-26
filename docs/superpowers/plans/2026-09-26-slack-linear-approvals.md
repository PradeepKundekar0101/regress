# Slack Approvals and Linear Issues Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Regress checkpoints an incident, the agent files a Linear issue and posts a Slack message with Approve and Reject buttons; a click answers the agent's pending TrueForge approval under the same checks as the console, and the outcome is reported back in the Slack thread and on the Linear issue.

**Architecture:** A new `regress_mcp/slack.py` owns every Slack message (Block Kit built from the frozen proposal, thread replies, the decided state) and is exposed to the agent as two regress-mcp tools. The console's decision logic moves into `console/decisions.py` with a store-backed first-decision lock, and `console/slack.py` receives button clicks over Socket Mode (slack_bolt) and routes them through that same function. Linear is the official hosted MCP, registered by `agent/bootstrap.py` and driven by the runbook.

**Tech Stack:** Python 3.11, FastAPI, httpx, SQLite, MCP (`mcp` 2.x MCPServer), slack-bolt (Socket Mode), Linear hosted MCP, TrueForge, pytest.

**Spec:** `docs/superpowers/specs/2026-09-26-slack-linear-approvals-design.md`

## Global Constraints

- Never use the em dash character in code, comments, docs or Slack copy; use a plain `-`.
- Markdown docs: one full sentence per physical line.
- Commit messages carry no agent co-author line other than the one in the session's attribution reminder.
- Only files named in a task are staged; the user has unrelated uncommitted edits in `console/app.py`, `console/static/index.html` and `target/bot/static/index.html`, so stage hunks you wrote with `git add -p` for those files and leave theirs unstaged.
- Slack buttons carry only the incident id; what is approved is always the store's frozen proposal checked against the pending TrueForge call.
- Slack and Linear are best-effort: a failure there never blocks the gated call or the console approval.
- The console approval stays; the first decision from either surface wins.
- New env vars: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL`, `SLACK_APPROVERS` (optional), `LINEAR_API_KEY`, `LINEAR_TEAM`, `CONSOLE_URL` (optional, default `http://localhost:8100`).
- `target/config.py` loads the real `.env` on import, so every test must clear the `SLACK_*` variables (done by an autouse fixture in Task 1).
- Run tests with `uv run pytest`.
- Spec deviation, on purpose: duplicate Slack deliveries are absorbed by the store's first-decision lock (Task 1) rather than a separate `(incident_id, action_ts)` table; a repeat click is told "already decided".
- The decider's name reaches TrueForge in the denial reason and the store's `decided_by`; the agent's own `record_decision` transition carries whatever reason it writes.

## File map

| File | Responsibility |
|---|---|
| `regress_mcp/store.py` | adds the `notifications` table and the first-decision lock |
| `regress_mcp/slack.py` (new) | Slack Web API calls and every incident message: approval request, thread updates, decided state |
| `regress_mcp/server.py` | exposes `request_approval` and `post_update` to the agent |
| `console/decisions.py` (new) | `apply_decision`, shared by the HTTP route and Slack |
| `console/slack.py` (new) | Socket Mode listener and the Approve, Reject and reason-modal handlers |
| `console/app.py` | route delegates to `decisions`; lifespan starts Slack; `/api/slack` status |
| `console/static/index.html` | Slack status chip in the top bar |
| `agent/bootstrap.py` | Linear connector, GitHub `issue_write` removed, Linear team in instructions |
| `skills/regress-runbook/SKILL.md` | Linear and Slack steps |
| `agent/preflight.py` | Slack and Linear checks |
| `docs/slack-app-manifest.yaml` (new), `.env.example`, `docs/demo-runbook.md`, `docs/playbook.md` | setup |
| `tests/conftest.py` (new), `tests/test_store.py`, `tests/test_slack_messages.py` (new), `tests/test_console.py`, `tests/test_slack_handlers.py` (new) | tests |

---

### Task 1: Notifications table and the first-decision lock

**Files:**
- Modify: `regress_mcp/store.py` (SCHEMA string near line 28; new methods after `latest_verified_replay`)
- Create: `tests/conftest.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `Store.notification(incident_id: str) -> dict | None` with keys `incident_id, channel, ts, summary, linear_url, decided_by, decided_at`
  - `Store.save_notification(incident_id: str, channel: str, ts: str, summary: str | None = None, linear_url: str | None = None) -> None` (upsert; keeps an existing decision and keeps existing summary/linear_url when passed None)
  - `Store.claim_decision(incident_id: str, actor: str) -> str | None` (None means claimed; otherwise returns who already holds it)
  - `Store.release_decision(incident_id: str) -> None`
  - `tests/conftest.py` autouse fixture `_no_real_slack` that clears `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL`, `SLACK_APPROVERS`

- [ ] **Step 1: Create the conftest that keeps the real `.env` out of tests**

```python
"""Shared test setup. target.config loads the real .env on import, so Slack settings are cleared for every test."""

import pytest

SLACK_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL", "SLACK_APPROVERS")


@pytest.fixture(autouse=True)
def _no_real_slack(monkeypatch):
    for name in SLACK_ENV:
        monkeypatch.delenv(name, raising=False)
```

- [ ] **Step 2: Write the failing store tests** (append to `tests/test_store.py`)

```python
def test_notification_round_trips_and_keeps_the_decision(store):
    inc = store.open_incident("eval_score", [])
    assert store.notification(inc) is None
    store.save_notification(inc, "C1", "100.1", summary="prompt v2 dropped escalation", linear_url="https://linear.app/x/1")
    assert store.claim_decision(inc, "@ana") is None
    store.save_notification(inc, "C1", "100.1")  # re-saving without prose keeps prose and decision
    n = store.notification(inc)
    assert (n["channel"], n["ts"], n["summary"], n["linear_url"], n["decided_by"]) == (
        "C1", "100.1", "prompt v2 dropped escalation", "https://linear.app/x/1", "@ana")
    assert n["decided_at"]


def test_only_the_first_decision_is_claimed(store):
    inc = store.open_incident("eval_score", [])
    assert store.claim_decision(inc, "@ana") is None
    assert store.claim_decision(inc, "console") == "@ana"
    store.release_decision(inc)
    assert store.claim_decision(inc, "console") is None
    assert store.notification(inc)["ts"] is None  # a console-only claim has no Slack message
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_store.py -k "notification or first_decision" -v`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'notification'`

- [ ] **Step 4: Add the table to `SCHEMA`** (after the `replays` table, inside the same string)

```sql
create table if not exists notifications (
  incident_id  text primary key references incidents(id),
  channel      text,          -- Slack channel id of the incident's message
  ts           text,          -- Slack ts of that message; replies thread under it
  summary      text,          -- the agent's summary, kept so the message can be redrawn once decided
  linear_url   text,
  decided_by   text,          -- first decider: "@slack-user" or "console"; the lock against a second decision
  decided_at   text
);
```

- [ ] **Step 5: Add the methods** (new section after `latest_verified_replay`, before `_incident_dict`)

```python
    # --- notifications and the one decision ---------------------------------------------

    def notification(self, incident_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("select * from notifications where incident_id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None

    def save_notification(self, incident_id: str, channel: str, ts: str, summary: str | None = None,
                          linear_url: str | None = None) -> None:
        """Remember the incident's Slack message. A recorded decision, summary or Linear link is kept."""
        with self._conn() as conn:
            conn.execute(
                "insert into notifications (incident_id, channel, ts, summary, linear_url) values (?, ?, ?, ?, ?) "
                "on conflict(incident_id) do update set channel = excluded.channel, ts = excluded.ts, "
                "summary = coalesce(excluded.summary, summary), linear_url = coalesce(excluded.linear_url, linear_url)",
                (incident_id, channel, ts, summary, linear_url),
            )

    def claim_decision(self, incident_id: str, actor: str) -> str | None:
        """Take the incident's one decision atomically. None if taken now, else who already holds it."""
        with self._conn() as conn:
            conn.execute("begin immediate")
            conn.execute("insert or ignore into notifications (incident_id) values (?)", (incident_id,))
            held = conn.execute("select decided_by from notifications where incident_id = ?",
                                (incident_id,)).fetchone()["decided_by"]
            if held:
                conn.execute("rollback")
                return held
            conn.execute("update notifications set decided_by = ?, decided_at = ? where incident_id = ?",
                          (actor, now_iso(), incident_id))
            conn.execute("commit")
        return None

    def release_decision(self, incident_id: str) -> None:
        """Undo a claim whose answer never reached TrueForge, so the decision can be made again."""
        with self._conn() as conn:
            conn.execute("update notifications set decided_by = null, decided_at = null where incident_id = ?",
                         (incident_id,))
```

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass (39 existing plus 2 new).

- [ ] **Step 7: Commit**

```bash
git add regress_mcp/store.py tests/conftest.py tests/test_store.py
git commit -m "Store: notifications table and a first-decision lock"
```

---

### Task 2: Slack messages module and the agent's two tools

**Files:**
- Create: `regress_mcp/slack.py`
- Modify: `regress_mcp/server.py` (imports at line 20, `EXPECTED` at line 40, new tools after `record_decision`)
- Modify: `tests/conftest.py` (add `FakeSlack`, `slack_api`, `checkpointed`)
- Test: `tests/test_slack_messages.py`

**Interfaces:**
- Consumes: Task 1 store methods.
- Produces (module `regress_mcp.slack`):
  - constants `APPROVE = "regress_approve"`, `REJECT = "regress_reject"`, `REJECT_VIEW = "regress_reject_reason"`
  - module attribute `transport: httpx.BaseTransport | None` (tests replace it)
  - `class SlackError(RuntimeError)`
  - `configured() -> bool`
  - `call(method: str, **payload) -> dict` (JSON POST to `https://slack.com/api/<method>`, raises `SlackError` when `ok` is false)
  - `proposal_text(proposal: dict) -> str`
  - `approval_blocks(incident: dict, summary: str, linear_url: str | None, decided: str | None = None) -> list[dict]`
  - `request_approval(store, incident_id, summary, linear_url=None) -> {"channel", "ts", "updated"}`
  - `post_update(store, incident_id, text) -> {"channel", "ts", "thread_ts"}`
  - `mark_decided(store, incident_id, decision: "allow"|"deny", actor: str, reason: str | None = None) -> bool`
- Produces (conftest): fixture `slack_api` returning a `FakeSlack` with `.calls: list[tuple[str, dict]]` and `.of(method) -> list[dict]`; fixture `checkpointed` returning `(store, incident_id)` for a checkpointed incident with `PROPOSAL`.

- [ ] **Step 1: Add shared fakes to `tests/conftest.py`** (append)

```python
import json

import httpx

from regress_mcp import slack
from regress_mcp.store import Store

PROPOSAL = {"action": "rollback_execute", "prompt": "adopt-support", "label": "production",
            "from_version": 2, "to_version": 1, "undo": "flip the production label back to v2",
            "blast_radius": {"affected_requests_in_window": 41, "requests_in_window": 41, "scope": "one prompt label"}}


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
```

Move the `import pytest` line so all imports sit at the top of the file.

- [ ] **Step 2: Write the failing tests** in `tests/test_slack_messages.py`

```python
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
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_slack_messages.py -v`
Expected: FAIL with `ImportError: cannot import name 'slack' from 'regress_mcp'`.

- [ ] **Step 4: Write `regress_mcp/slack.py`**

```python
"""Slack messages for incidents: the approval request with its buttons, thread updates and the decided state.

The buttons are built here from the incident's frozen proposal and carry only the incident id, so a click
can approve only what the store froze. The agent supplies prose, never Block Kit.
"""

import os
from datetime import datetime, timezone

import httpx

from regress_mcp.store import Store

API = "https://slack.com/api/"
APPROVE, REJECT, REJECT_VIEW = "regress_approve", "regress_reject", "regress_reject_reason"
transport: httpx.BaseTransport | None = None  # tests swap in a MockTransport


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
    if existing and existing["ts"]:
        call("chat.update", channel=existing["channel"], ts=existing["ts"], text=text, blocks=blocks)
        channel, ts, updated = existing["channel"], existing["ts"], True
    else:
        posted = call("chat.postMessage", channel=os.environ["SLACK_CHANNEL"], text=text, blocks=blocks)
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
    posted = call("chat.postMessage", channel=os.environ["SLACK_CHANNEL"], text=f"*Regress {incident_id}*\n{text}")
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
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_slack_messages.py -v`
Expected: 9 passed.

- [ ] **Step 6: Expose the tools in `regress_mcp/server.py`**

Change the import on line 20 to include `slack`:

```python
from regress_mcp import actions, detector, gates, localize as localize_mod, narrative, replay, slack, sources
```

After the `WRITE = ...` annotation line add:

```python
NOTIFY = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
```

Change `EXPECTED` so Slack failures reach the agent as readable refusals:

```python
EXPECTED = (actions.ActionRefused, TransitionError, ValueError, KeyError, slack.SlackError)
```

In `INSTRUCTIONS`, replace the flow line fragment `validate_narrative -> rollback_execute or route_revert (needs human` and the next line with:

```python
submit_replay_report -> check_gates -> validate_narrative -> request_approval (Slack) -> rollback_execute or
route_revert (needs human approval) -> verify_recovery -> post_update. NOT_LOCALIZED and INSUFFICIENT_DATA are valid endings."""
```

(the full string must read: `Flow: run_detector -> record_plan -> localize -> get_traces -> replay_generate -> (score in sandbox) ->` then the two lines above.)

After `record_decision` add:

```python
@tool(NOTIFY)
def request_approval(incident_id: str, summary: str, linear_url: str | None = None) -> dict:
    """Ask for the human decision in Slack: your summary plus Approve/Reject buttons built from the frozen proposal.

    Call after check_gates returned checkpointed and before the gated tool. The summary is validator-rendered
    text (no {{ev_id}}). Pass the Linear issue URL if you filed one. Calling again updates the same message.
    """
    return slack.request_approval(store, incident_id, summary, linear_url)


@tool(NOTIFY)
def post_update(incident_id: str, text: str) -> dict:
    """Reply in the incident's Slack thread, or start one if none exists: outcomes, a denial's next branch,
    or a NOT_LOCALIZED / INSUFFICIENT_DATA ending. Text is validator-rendered (no {{ev_id}})."""
    return slack.post_update(store, incident_id, text)
```

- [ ] **Step 7: Check the server still imports and lists the tools**

Run: `uv run python -c "import anyio; from regress_mcp import server; print(sorted(t.name for t in anyio.run(server.mcp.list_tools)))"`
Expected: the list includes `post_update` and `request_approval`. If `list_tools` has a different name on this MCPServer version, instead start `uv run python -m regress_mcp.server` and run `uv run python -m agent.bootstrap` later in Task 5, which prints the tool count.

- [ ] **Step 8: Run the whole suite and commit**

Run: `uv run pytest -q` (expect all pass)

```bash
git add regress_mcp/slack.py regress_mcp/server.py tests/conftest.py tests/test_slack_messages.py
git commit -m "regress-mcp: Slack approval request and thread updates built from the frozen proposal"
```

---

### Task 3: One decision path for the console and Slack

**Files:**
- Create: `console/decisions.py`
- Modify: `console/app.py` (remove `GATED_TOOLS` at line 26, `_proposal_args` and the body of `decide`)
- Test: `tests/test_console.py`

**Interfaces:**
- Consumes: `Store.notification`, `Store.claim_decision`, `Store.release_decision` (Task 1); `slack.mark_decided`, `slack.SlackError` (Task 2).
- Produces (module `console.decisions`):
  - `class DecisionRefused(Exception)` with attribute `retryable: bool`
  - `apply_decision(store: Store, tf_client: Callable[[], httpx.Client], incident_id: str, decision: str, reason: str | None, actor: str) -> dict` returning `{"ok", "decision", "turn_id", "session_url"}`; raises `KeyError` for an unknown incident and `DecisionRefused` for every refusal. `retryable=True` only when no session is linked or no gated call is pending yet.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_console.py`; also give `FakeTrueForge` a failing mode)

Replace the `FakeTrueForge` class header and POST branch with:

```python
class FakeTrueForge:
    def __init__(self, call: dict | None, fail_post: bool = False):
        self.call, self.posted, self.fail_post = call, [], fail_post
```

```python
        if path.endswith("/turns") and request.method == "POST":
            if self.fail_post:
                return httpx.Response(500, json={"error": "boom"})
            self.posted.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"id": "turn_2"}})
```

New tests:

```python
def test_console_decision_updates_the_slack_message(env, slack_api):
    store, inc, use = env
    store.save_notification(inc, "C1", "100.1", summary="summary")
    resp = use(FakeTrueForge(pending_call(good_args(inc)))).post(f"/api/incidents/{inc}/decision", json={"decision": "allow"})
    assert resp.status_code == 200, resp.text
    [upd] = slack_api.of("chat.update")
    assert upd["ts"] == "100.1" and "Approved* by console" in str(upd["blocks"])
    assert not [b for b in upd["blocks"] if b["type"] == "actions"]


def test_second_decision_names_who_decided(env):
    store, inc, use = env
    store.claim_decision(inc, "@ana")
    fake = FakeTrueForge(pending_call(good_args(inc)))
    resp = use(fake).post(f"/api/incidents/{inc}/decision", json={"decision": "deny", "reason": "late"})
    assert resp.status_code == 409 and "already decided by @ana" in resp.text and fake.posted == []


def test_failed_answer_releases_the_decision(env):
    store, inc, use = env
    with pytest.raises(httpx.HTTPStatusError):
        use(FakeTrueForge(pending_call(good_args(inc)), fail_post=True)).post(
            f"/api/incidents/{inc}/decision", json={"decision": "allow"})
    assert store.notification(inc)["decided_by"] is None


def test_slack_denial_reason_names_the_decider(env):
    from console import decisions
    store, inc, use = env
    fake = FakeTrueForge(pending_call(good_args(inc)))
    console_app.state["tf_transport"] = httpx.MockTransport(fake)
    decisions.apply_decision(store, console_app.tf_client, inc, "deny", "not at peak", actor="@ana")
    assert fake.posted[0]["input"][0]["approval"] == {"status": "deny", "reason": "not at peak (rejected by @ana in Slack)"}
    assert store.notification(inc)["decided_by"] == "@ana"


def test_not_paused_yet_is_retryable(env):
    from console import decisions
    store, inc, use = env
    console_app.state["tf_transport"] = httpx.MockTransport(FakeTrueForge(None))
    with pytest.raises(decisions.DecisionRefused) as exc:
        decisions.apply_decision(store, console_app.tf_client, inc, "allow", None, actor="@ana")
    assert exc.value.retryable is True
    assert (store.notification(inc) or {}).get("decided_by") is None
```

Keep the local `PROPOSAL` in `tests/test_console.py`; its `blast_radius: {}` also exercises `proposal_text` without a blast radius.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_console.py -v`
Expected: the five new tests FAIL (`ModuleNotFoundError: console.decisions`, no Slack update, no lock); the existing tests (including the user's `test_page_and_icons_are_served`) still pass.

- [ ] **Step 3: Write `console/decisions.py`**

```python
"""Answering the agent's one pending approval, from the console or from Slack, under the same checks.

A decision is refused unless the incident is checkpointed, exactly one gated call is pending and its
arguments equal the frozen proposal. The first decision takes a lock in the store, so a second click from
either surface is refused with who decided.
"""

import logging
from collections.abc import Callable

import httpx

from console import trueforge
from regress_mcp import slack
from regress_mcp.store import Store

GATED_TOOLS = {"rollback_execute", "route_revert"}
log = logging.getLogger("regress.decisions")


class DecisionRefused(Exception):
    """Not answered. `retryable` means the agent has not paused on the call yet, so asking again soon may work."""

    def __init__(self, detail: str, retryable: bool = False):
        super().__init__(detail)
        self.retryable = retryable


def _proposal_args(proposal: dict) -> tuple[str, dict]:
    keys = {"rollback_execute": ("prompt", "label", "from_version", "to_version"),
            "route_revert": ("route", "from_model", "to_model")}[proposal["action"]]
    return proposal["action"], {k: proposal[k] for k in keys}


def apply_decision(store: Store, tf_client: Callable[[], httpx.Client], incident_id: str, decision: str,
                   reason: str | None, actor: str) -> dict:
    inc = store.incident(incident_id)
    held = (store.notification(incident_id) or {}).get("decided_by")
    if held:
        raise DecisionRefused(f"{incident_id} was already decided by {held}")
    if inc["status"] != "checkpointed" or not inc["proposal"]:
        raise DecisionRefused(f"{incident_id} is {inc['status']}; only a checkpointed incident awaits a decision")
    with tf_client() as h:
        sid = trueforge.session_for(incident_id, h)
        if sid is None:
            raise DecisionRefused("no TrueForge session is linked to this incident", retryable=True)
        view = trueforge.session_view(h, sid)
        gated = [p for p in view["pending"] if p["tool"] in GATED_TOOLS]
        if not gated:
            raise DecisionRefused("Regress has not paused on the approval yet", retryable=True)
        if len(gated) > 1:
            raise DecisionRefused(f"expected exactly one pending gated call, found {len(gated)}")
        pending = gated[0]
        tool, expected = _proposal_args(inc["proposal"])
        actual = {k: pending["arguments"].get(k) for k in expected}
        if pending["tool"] != tool or actual != expected or pending["arguments"].get("incident_id") != incident_id:
            raise DecisionRefused(f"the pending call does not match the frozen proposal: {pending['tool']} {actual}")
        held = store.claim_decision(incident_id, actor)
        if held:
            raise DecisionRefused(f"{incident_id} was already decided by {held}")
        note = reason if actor == "console" or decision == "allow" else f"{reason or 'no reason given'} (rejected by {actor} in Slack)"
        try:
            turn_id = trueforge.answer(h, sid, pending, decision, note)
        except Exception:
            store.release_decision(incident_id)
            raise
    try:
        slack.mark_decided(store, incident_id, decision, actor, reason)
    except (slack.SlackError, httpx.HTTPError) as exc:  # the decision is made; a stale Slack message is cosmetic
        log.warning("could not update the Slack message for %s: %s", incident_id, exc)
    return {"ok": True, "decision": decision, "turn_id": turn_id, "session_url": view["url"]}
```

- [ ] **Step 4: Point the route at it in `console/app.py`**

Delete `GATED_TOOLS = {...}` (line 26) and `_proposal_args`. Add `decisions` to the console import: `from console import decisions, trueforge`. Replace the whole `decide` function with:

```python
@app.post("/api/incidents/{incident_id}/decision")
def decide(incident_id: str, body: Decision) -> dict:
    try:
        return decisions.apply_decision(store(), tf_client, incident_id, body.decision, body.reason, actor="console")
    except KeyError:
        raise HTTPException(404, f"unknown incident {incident_id}")
    except decisions.DecisionRefused as exc:
        raise HTTPException(409, str(exc))
```

Update the module docstring's last sentence to: `Its only write is answering an approval the agent already asked for (here or from Slack, see console/decisions.py).`

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit** (stage only your hunks in `console/app.py`)

```bash
git add console/decisions.py tests/test_console.py
git add -p console/app.py
git commit -m "Console: one decision path with a first-decision lock, shared with Slack"
```

---

### Task 4: Slack buttons over Socket Mode, and the console status chip

**Files:**
- Create: `console/slack.py`
- Modify: `console/app.py` (lifespan, `/api/slack`)
- Modify: `console/static/index.html` (top bar chip, `tick`)
- Modify: `pyproject.toml`, `uv.lock` (via `uv add`)
- Test: `tests/test_slack_handlers.py`

**Interfaces:**
- Consumes: `DecisionRefused`, `apply_decision` (Task 3); `slack.call`, `slack.APPROVE`, `slack.REJECT`, `slack.REJECT_VIEW` (Task 2).
- Produces (module `console.slack`):
  - `Apply = Callable[[str, str, str | None, str], dict]` (incident_id, decision, reason, actor)
  - `decide_with_retry(apply, incident_id, decision, reason, user: dict, channel: str, *, sleep=time.sleep, wait_s=20.0, every_s=2.0) -> dict | None`
  - `handle_approve(body, apply, **retry)`, `handle_reject(body)`, `handle_reject_submit(body, apply, **retry)`
  - `start(apply) -> None`, `stop() -> None`, `status() -> "off" | "connected" | "disconnected" | "error"`
  - HTTP `GET /api/slack -> {"status": ...}`

- [ ] **Step 1: Add the dependency**

Run: `uv add slack-bolt`
Expected: `pyproject.toml` gains `slack-bolt>=...` and `uv.lock` updates.

- [ ] **Step 2: Write the failing tests** in `tests/test_slack_handlers.py`

```python
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
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_slack_handlers.py -v`
Expected: FAIL with `ImportError: cannot import name 'slack' from 'console'`.

- [ ] **Step 4: Write `console/slack.py`**

```python
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
```

- [ ] **Step 5: Run the handler tests**

Run: `uv run pytest tests/test_slack_handlers.py -v`
Expected: 8 passed.

- [ ] **Step 6: Wire it into `console/app.py`**

Add imports: `from contextlib import asynccontextmanager` and `from console import slack as slack_bridge` (keep `decisions, trueforge` import). Replace `app = FastAPI(title="Regress console")` with:

```python
@asynccontextmanager
async def lifespan(_: FastAPI):
    slack_bridge.start(lambda incident_id, decision, reason, actor: decisions.apply_decision(
        store(), tf_client, incident_id, decision, reason, actor))
    yield
    slack_bridge.stop()


app = FastAPI(title="Regress console", lifespan=lifespan)
```

`store` and `tf_client` are defined below `app`; the lambda resolves them at call time, so order is fine.

Add after `index()`:

```python
@app.get("/api/slack")
def slack_status() -> dict:
    return {"status": slack_bridge.status()}
```

- [ ] **Step 7: Add the status chip to `console/static/index.html`**

In the top bar, change the `.right` div to:

```html
      <div class="right"><span class="chip" id="top-slack" hidden></span><span class="beacon" id="top-beacon"></span><span id="top-live">Connecting</span></div>
```

Before `async function tick()` add:

```js
const SLACK_LABELS = { connected: ["ok", "Slack connected"], disconnected: ["bad", "Slack disconnected"], error: ["bad", "Slack not connected"] };
async function refreshSlack() {
  const { status } = await getJSON("/api/slack");
  const chip = $("top-slack");
  chip.hidden = !SLACK_LABELS[status];
  if (!chip.hidden) { chip.className = `chip ${SLACK_LABELS[status][0]}`; chip.textContent = SLACK_LABELS[status][1]; }
}
```

In `tick()`, as the first line inside `try {`, add `refreshSlack().catch(() => {});`.

- [ ] **Step 8: Verify in the browser**

Start the console via the preview tool (`.claude/launch.json` entry `console`: `uv run uvicorn console.app:app --port 8100`), load `http://localhost:8100`, and confirm with `read_console_messages` that there are no errors. With no Slack tokens the chip is hidden; `curl -s localhost:8100/api/slack` returns `{"status":"off"}`. Check both light and dark themes once tokens exist (Task 6) and the chip reads "Slack connected" in the ok colours, aligned with the beacon.

- [ ] **Step 9: Run the whole suite and commit**

Run: `uv run pytest -q` (all pass)

```bash
git add console/slack.py tests/test_slack_handlers.py pyproject.toml uv.lock
git add -p console/app.py console/static/index.html
git commit -m "Console: Slack Approve/Reject over Socket Mode with a reason modal and status chip"
```

---

### Task 5: Linear connector, runbook, and setup docs

**Files:**
- Modify: `agent/bootstrap.py`
- Modify: `skills/regress-runbook/SKILL.md`
- Modify: `agent/preflight.py`
- Create: `docs/slack-app-manifest.yaml`
- Modify: `.env.example`, `docs/demo-runbook.md`, `docs/playbook.md`

**Interfaces:**
- Consumes: regress-mcp tools `request_approval`, `post_update` (Task 2); `GET /api/slack` (Task 4).
- Produces: TrueForge connector `linear`; agent manifest with `linear` tools minus delete/archive/remove; GitHub without `issue_write`.

- [ ] **Step 1: Register Linear in `agent/bootstrap.py`**

Change `GITHUB_TOOLS` and its comment:

```python
# GitHub: read the config history and comment on existing issues. Incident tickets live in Linear, and nothing
# that creates issues, pushes, deletes, merges or creates repositories is exposed.
GITHUB_TOOLS = ["list_commits", "get_commit", "get_file_contents", "list_issues", "issue_read", "add_issue_comment"]
# Linear's hosted MCP files and updates the incident issue. Tool names are read from the server at bootstrap;
# anything that deletes, archives or removes is left out.
LINEAR_URL = "https://mcp.linear.app/mcp"
LINEAR_BLOCKED = ("delete", "archive", "remove")
```

Add after `register_posthog_connector`:

```python
def register_linear_connector(h: httpx.Client) -> list[str]:
    """Linear's official MCP with an API key (registered through the API: the form turns headers into Bearer Bearer)."""
    key = config.optional_env("LINEAR_API_KEY")
    if not key:
        print("connector linear: skipped (LINEAR_API_KEY not set)")
        return []
    _check(h.put(f"{TRUEFORGE}/settings/mcp-servers", json={"manifest": {
        "type": "remote", "name": "linear", "url": LINEAR_URL,
        "description": "Linear: file the incident issue with Regress's report, comment outcomes, close it once verified",
        "auth": {"type": "header", "headers": {"Authorization": f"Bearer {key}"}},
    }}))
    tools = _check(h.get(f"{TRUEFORGE}/mcp-servers/linear/tools"))
    names = sorted(t["name"] for t in (tools["data"]["tools"] if isinstance(tools["data"], dict) else tools["data"]))
    allowed = [n for n in names if not any(b in n.lower() for b in LINEAR_BLOCKED)]
    print(f"connector linear: {len(allowed)} of {len(names)} tools: {', '.join(allowed)}")
    return allowed
```

Change `manifest` to take the Linear tools and name the team:

```python
def manifest(available: set[str], linear_tools: list[str]) -> dict:
```

Inside it, after the GitHub block:

```python
    instructions = INSTRUCTIONS
    if "linear" in available and linear_tools:
        servers.append({"name": "linear", "enable_tools": linear_tools, "require_approval_for_tools": ["@destructive"]})
        instructions += f"\nFile incident issues in the Linear team {config.env('LINEAR_TEAM')!r}."
```

and use `"instructions": instructions,` in the returned dict.

Add to `INSTRUCTIONS`, as a new bullet before `Be brief in chat`:

```
- Ask for the decision in Slack with request_approval before the gated call, and report every ending with post_update.
  Linear and Slack are best-effort: if one fails, say so in one line and carry on.
```

In `main()`, call `linear_tools = register_linear_connector(h)` after `register_posthog_connector(h)` and pass it: `spec = manifest(available, linear_tools)`.

- [ ] **Step 2: Update the runbook `skills/regress-runbook/SKILL.md`**

Replace hard rules 5 and 6 and add rule 8:

```markdown
5. If a human denies the approval, call `record_decision(decision="denied")`, then `post_update` and a Linear comment naming the next branch you would investigate, and stop.
6. Every report you show a human or post to Linear or Slack is the validator's `rendered` text, never the `{{ev_id}}` draft.
```

```markdown
8. Linear and Slack are best-effort. If a call to either fails, say so in one line and carry on; never skip or delay the gated call because of them, since the console can always approve.
```

Replace procedure steps 7 and 9:

```markdown
7. **Propose.** If checkpointed:
   1. File the Linear issue in the team named in your instructions: title `Regress <incident_id>: <verdict>`, description the validator's `rendered` report.
   2. Call `request_approval(incident_id, summary, linear_url)`; the summary is at most five lines of the rendered report (what happened, cause, proposed action).
   3. Post the rendered report in chat.
   4. Call the gated tool with the proposal's arguments. TrueForge pauses until a human approves in Slack or the console.
```

```markdown
9. **Close.** Write the outcome with `{{ev_id}}` placeholders from `verify_recovery`'s evidence (metric before and after), run it through `validate_narrative`, then:
   - `post_update(incident_id, rendered)` in the Slack thread;
   - comment the same text on the Linear issue;
   - if `verified`, move the issue to the team's completed state (look it up); if `verify_failed`, leave it open.
   For NOT_LOCALIZED or INSUFFICIENT_DATA, file the Linear issue with the rendered report and `post_update` a two-line rendered summary; no approval is requested.
   End with the verdict.
```

In "Restart and resume", change the `checkpointed` sentence to:

```markdown
`checkpointed` means call `request_approval` again (it updates the same Slack message), then re-issue the gated call with the frozen proposal (the tool reads the live state and never flips twice);
```

- [ ] **Step 3: Write `docs/slack-app-manifest.yaml`**

```yaml
# Create the Slack app at https://api.slack.com/apps > Create New App > From a manifest, paste this, then:
# 1. Basic Information > App-Level Tokens > Generate, scope connections:write -> SLACK_APP_TOKEN (xapp-...)
# 2. Install to Workspace -> OAuth & Permissions > Bot User OAuth Token -> SLACK_BOT_TOKEN (xoxb-...)
# 3. In the channel: /invite @Regress; channel details > copy the channel id -> SLACK_CHANNEL (C...)
display_information:
  name: Regress
  description: On-call agent for the Adopt.ai support bot. Asks here before it changes production.
  background_color: "#5b3fd1"
features:
  bot_user:
    display_name: Regress
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write
      - channels:read
      - groups:read
settings:
  interactivity:
    is_enabled: true
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

- [ ] **Step 4: Add the settings to `.env.example`** (append)

```bash

# Slack approvals (docs/slack-app-manifest.yaml). Bot token xoxb-, app-level token xapp- (Socket Mode),
# channel id C..., optional comma-separated user ids allowed to decide, and where Slack links to the console.
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
SLACK_CHANNEL=
SLACK_APPROVERS=
CONSOLE_URL=http://localhost:8100

# Linear (Settings > Security & access > Personal API keys) and the team incident issues go to, by name or key
LINEAR_API_KEY=
LINEAR_TEAM=
```

- [ ] **Step 5: Add preflight checks to `agent/preflight.py`** (after `_posthog`)

```python
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
```

- [ ] **Step 6: Update the docs**

In `docs/demo-runbook.md`:
- section 2: "All ten lines" becomes "All thirteen lines", and append ", Slack bot in the channel, console connected to Slack, TrueForge reaches Linear" to the list sentence.
- section 3: replace window 4 with `4. **Slack** the approval channel, and **Linear** the team's issue list (GitHub commits stay one tab over).`
- `3:15 Approve`: `Do: click **Approve** in Slack (confirm the dialog); the console and TrueForge still work.` and add `See: the Slack message turns into "Approved by @you"; after verification a thread reply with before/after numbers and the Linear issue moves to Done.` Replace "a GitHub issue with the report" with "the Linear issue with the report".
- `4:15 Second fault`: `Do: **Reject** in Slack, type the reason in the dialog, submit.` and `See: the message shows "Rejected by @you: <reason>", a thread reply names the next branch, the Linear issue stays open with the comment.`
- the `1:00` narration: replace "and GitHub for the change history and the report" with "GitHub for the change history, and Linear and Slack for the ticket and the human decision".

In `docs/playbook.md`, "Before 12:00": replace the GitHub connector line with the two lines below and add a Slack line:

```markdown
- [ ] GitHub connector added in TrueForge (catalog, fine-grained PAT with Contents read on the config repo; issues now live in Linear).
- [ ] Linear API key and team in `.env`; `agent.bootstrap` registers the Linear MCP through the API (never the form).
- [ ] Slack app from `docs/slack-app-manifest.yaml`, bot invited to the channel, three tokens in `.env`; the console header says "Slack connected".
```

- [ ] **Step 7: Publish and bootstrap** (needs `.env` filled by the user; skip to Task 6's prerequisites if not)

Run: `uv run python -m agent.bootstrap`
Expected: `connector linear: N of M tools: ...` listing issue and comment tools and no delete/archive/remove; `connectors attached: [..., 'linear', ...]`; skill published with a new SHA.

- [ ] **Step 8: Run the suite and commit**

Run: `uv run pytest -q` (all pass)

```bash
git add agent/bootstrap.py agent/preflight.py skills/regress-runbook/SKILL.md docs/slack-app-manifest.yaml .env.example docs/demo-runbook.md docs/playbook.md
git commit -m "Linear MCP for incident issues; runbook asks for approval in Slack and reports outcomes"
```

---

### Task 6: End-to-end verification with real Slack and Linear

**Prerequisites from the user** (Claude does not enter tokens or create accounts): the Slack app created from the manifest, the bot invited to the channel, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL`, `LINEAR_API_KEY`, `LINEAR_TEAM` in `.env`.

- [ ] **Step 1: Start everything** per `docs/demo-runbook.md` section 1 (TrueForge, regress-mcp, bot, console, traffic, watcher), then `uv run python -m agent.bootstrap`.

- [ ] **Step 2: Preflight**

Run: `uv run python -m agent.preflight`
Expected: all thirteen PASS (the detector needs 30 minutes of clean traffic first).

- [ ] **Step 3: Approve path**

Run: `./scripts/fault_prompt.sh && sleep 12 && uv run python -m target.traffic --burst 50`
Then check, in order:
- watcher prints `ALARM ... opened inc_...`;
- a Linear issue `Regress inc_...: LOCALIZED_PROMPT` exists with the rendered report (open it in the browser pane);
- the Slack channel has the message with the proposal line `Roll back prompt adopt-support label production from v2 to v1`, blast radius, undo, Linear and console links, and two buttons;
- click **Approve** in Slack, confirm; then `uv run python -m target.traffic --burst 30`;
- the message turns into "Approved by @you at HH:MM UTC" with no buttons; the console timeline shows `approved -> applied -> verified`; a thread reply carries before and after numbers; the Linear issue has the comment and is Done.

- [ ] **Step 4: Reject path**

Run: `./scripts/fault_route.sh && sleep 7 && uv run python -m target.traffic --burst 40`
Then: click **Reject**, enter a reason, submit; the message shows "Rejected by @you: <reason>"; TrueForge shows the denial reason ending in `(rejected by @you in Slack)`; the incident is `denied`; the thread reply names the next branch; the Linear issue stays open with the comment.
Reset with `./scripts/revert_route.sh`.

- [ ] **Step 5: Edge checks**

- Click Approve on the (now decided) route message again if Slack still shows it anywhere: the ephemeral says it was already decided.
- Stop the console mid-demo and start it again: the chip returns to "Slack connected" within a few seconds.

- [ ] **Step 6: Record learnings** in `docs/learnings.md` (actual Linear tool names, anything that surprised us), one sentence per line, and commit:

```bash
git add docs/learnings.md
git commit -m "Learnings from the Slack and Linear end-to-end run"
```
