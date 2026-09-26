# Slack approvals and Linear issues for Regress incidents

Date: 2026-09-26

## Goal

When Regress reports an incident and proposes a fix, the agent files a Linear issue and posts to Slack.
Slack shows the incident with Approve and Reject buttons, and a click answers the agent's pending approval exactly as the console does today.
After the decision, the Slack thread and the Linear issue record the outcome.

## Decisions

- Slack button clicks reach the local console through Socket Mode (slack_bolt), so no public URL or tunnel is needed.
- The agent acts, not background code: it files the Linear issue through the official Linear MCP and posts to Slack through new regress-mcp tools.
- The Slack message and its buttons are built deterministically by a regress-mcp tool from the frozen proposal, never from agent-written Block Kit.
- Linear replaces the GitHub issue step in the runbook, so each incident has one ticket.
- The console approval stays; Slack is a second surface for the same decision, and the first decision wins.

## Components

### Linear MCP connector

- The official hosted Linear MCP (`https://mcp.linear.app/mcp`) is registered in `agent/bootstrap.py` through the TrueForge API, as the Langfuse connector is, to avoid the UI's Bearer-prefix bug.
- Auth is `LINEAR_API_KEY`.
- The agent gets an allowlist only: issue create, issue update, comment create, and the team, state and label lookups it needs.
- No delete or archive tools are exposed.
- `issue_write` is removed from the agent's GitHub tool allowlist.

### regress-mcp tools

`request_approval(incident_id, summary, linear_url=None)`:

- Refuses unless the incident is `checkpointed` with a frozen proposal.
- Posts to `SLACK_CHANNEL` a message with the agent's summary, the proposal read from the store, a Linear link when given, a console link, and Approve and Reject buttons whose value is only the `incident_id`.
- Saves `channel` and `ts` in a new `notifications` table.
- If a message already exists for the incident, it updates that message instead of posting a new one.
- On Slack failure it returns an error result; the runbook continues regardless.

`post_update(incident_id, text)`:

- Replies in the incident's Slack thread.
- If the incident has no message yet (for example `not_localized`), it posts a new top-level message and saves its `ts`.

### Store

A new `notifications` table in the incident store:

| column | meaning |
|---|---|
| `incident_id` | primary key, references `incidents(id)` |
| `channel` | Slack channel id |
| `ts` | Slack message timestamp of the approval message |
| `decided_by` | Slack user or `console` |
| `decided_at` | ISO timestamp |
| `decided_call` | the pending TrueForge `tool_call_id` the decision answered; the lock is per call, so a call re-issued after a resume can be decided again |

### Console

- The body of `POST /api/incidents/{id}/decision` moves into a plain function `apply_decision(incident_id, decision, reason, actor)` that both the HTTP route and the Slack handler call.
- All existing checks stay: incident is `checkpointed`, exactly one pending gated call, and its arguments equal the frozen proposal.
- After a successful decision from either surface, the Slack message (if any) is updated to show who decided and when, with the buttons removed.

### `console/slack.py`

- A slack_bolt app in Socket Mode, started in a background thread from the console's FastAPI lifespan, only when `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN` are set.
- Approve: calls `apply_decision(incident_id, "allow", None, actor=<slack user>)` and updates the message to "Approved by @user at HH:MM".
- Reject: opens a modal asking for a reason; on submit calls `apply_decision(incident_id, "deny", reason, actor)` and updates the message to "Rejected by @user: reason".
- The Slack user's display name is included in the decision reason, so it is recorded in TrueForge and in the incident transitions.
- The console header shows a small Slack connection status (connected, disconnected, not configured).

## Runbook changes

- Checkpointed: create the Linear issue with the validated report, call `request_approval` with a short summary and the issue URL, then call the gated tool.
- Verified or verify_failed: `post_update` with the before and after numbers, comment on the Linear issue, and move it to Done only when verified.
- Denied: `record_decision`, `post_update` naming the next branch, comment on the Linear issue, and leave it open.
- `not_localized` or `insufficient_data`: file a Linear issue and `post_update` a top-level summary; no approval is requested.
- Slack and Linear calls are best-effort: a failure is noted and the incident flow continues.

## Failure handling and ordering

- Click before the agent pauses: `apply_decision` finds no pending call; the handler shows "Waiting for Regress to pause" and retries every 2 seconds for up to 20 seconds, then replies ephemerally with the reason and leaves the buttons live.
- Proposal mismatch or any other refusal: ephemeral reply with the reason, buttons stay.
- Second decision from either surface: refused because the incident is no longer `checkpointed`; the clicker is told who already decided.
- Duplicate Slack deliveries are ignored by `(incident_id, action_ts)`.
- Slack disconnects: bolt reconnects; the console logs it and shows the status.
- Optional `SLACK_APPROVERS` (comma-separated Slack user ids) restricts who may decide; unset means anyone in the channel.
- The button carries only the incident id, so a crafted payload cannot change what is approved.

## Configuration

New entries in `.env.example`: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL`, `SLACK_APPROVERS`, `LINEAR_API_KEY`, `LINEAR_TEAM`.
New dependency: `slack-bolt`.
`docs/slack-app-manifest.yaml` creates the Slack app with Socket Mode, interactivity and the `chat:write` scope.
`agent/preflight.py` checks the Slack and Linear credentials and that the bot can post to the channel.
`docs/demo-runbook.md` and `docs/playbook.md` gain the setup steps.

## Testing

Unit tests, with TrueForge and Slack faked through `httpx.MockTransport` or stubbed clients as in `tests/test_console.py`:

- `tests/test_slack.py`: approve path, reject modal and deny with reason, click-before-pause retry, proposal mismatch refusal, already-decided, duplicate delivery, approver allowlist.
- `tests/test_console.py`: a console decision also updates the Slack message.
- regress-mcp tools: `request_approval` refuses unless checkpointed, builds buttons from the stored proposal, and updates instead of reposting; `post_update` threads or starts a message.
- `tests/test_store.py`: the `notifications` table round-trips.

End to end, before calling it done:

1. Inject a fault with `scripts/fault_prompt.sh` and let the watcher open an incident.
2. Confirm the agent files a real Linear issue and posts the approval message to a real Slack channel.
3. Approve in Slack; confirm the rollback applies, the thread gets the verified result and the Linear issue moves to Done.
4. Repeat with Reject and a reason; confirm the deny path in TrueForge, Slack and Linear.

## Out of scope

- Slack slash commands or chat with the agent from Slack.
- Paging or escalation when nobody decides.
- Creating Linear projects, cycles or custom workflows.
