# Demo runbook

How to set up, run, check and narrate the Regress demo end to end.
Every step below was run for real in the rehearsal; timings are what we measured.

## 1. Start everything (about 5 minutes, at least 2 hours before the demo)

One terminal tab per process, all from the repo root:

| Tab | Command | Healthy when |
|---|---|---|
| 1 TrueForge | `OUTBOUND_URL_ALLOWED_HOSTS='["127.0.0.1","localhost"]' npx @truefoundry/trueforge` | UI loads at http://localhost:8790 |
| 2 regress-mcp | `uv run python -m regress_mcp.server` | `Uvicorn running on http://127.0.0.1:8941` |
| 3 Bot | `uv run uvicorn target.bot.app:app --port 8000` | http://localhost:8000 shows Ada |
| 4 Console | `uv run uvicorn console.app:app --port 8100` | http://localhost:8100 loads |
| 5 Traffic | `uv run python -m target.traffic` | a `batch ... eval=1.00` line every 1-2 min |
| 6 Watcher | `WATCH_INTERVAL_SECONDS=20 uv run python -m agent.watcher` | `quiet (9 signals in band)` |
| 7 Demo | free tab for the fault commands | |
| 8 Awake | `caffeinate -dimsu` | the machine never sleeps (a sleep empties the detector baseline) |

If you changed the skill or agent: `uv run python -m agent.bootstrap`.
Leave traffic running: the detector needs 30 minutes of clean history, ideally 2 hours.
Do not trip faults in the last 2 hours except full rehearsals that open incidents.

## 2. Preflight (T-15 minutes)

```bash
uv run python -m agent.preflight
```

All ten lines must say PASS: bot answers, production on baseline, detector warm and quiet, no open incident, TrueForge has the model and agent, TrueForge reaches regress-mcp, TrueForge reaches PostHog MCP read-only, sandbox ready, console up, traffic flowing.
Also check by hand: OpenAI credits, and the Daytona dashboard has no pile of old sandboxes.

## 3. Open these windows, left to right

1. **Bot UI** http://localhost:8000 (the customer's view).
2. **Console** http://localhost:8100 (your main screen).
3. **TrueForge** http://localhost:8790 > Sessions (the agent at work).
4. **GitHub** https://github.com/PradeepKundekar0101/adopt-support-bot commits and issues.
5. **Terminal**: tab 6 (watcher) visible, tab 7 ready.

## 4. The demo (about 6 minutes)

### 0:00 Normal day

Do: in the bot UI ask "There is a charge on my card I did not make."
See: amber "Passed to a specialist" header, source `kb-10`, footer `prompt v1 · gpt-4.1-mini`.
Console header: all signals in band.
Say: "This is Ada, a fintech support bot. Traffic flows through it all day; every reply is scored against a golden set and traced in Langfuse. Fraud reports go straight to a human."

### 0:30 Someone ships a harmless-looking prompt change

Do (tab 7):
```bash
./scripts/fault_prompt.sh && sleep 12 && uv run python -m target.traffic --burst 50
```
See: `production v1 -> v2 ... commit`, then `eval≈0.5 format_valid≈20%`.
Do: ask the fraud question again in the bot UI.
See: a warmer answer, no specialist header, no source, footer `prompt v2`.
Say: "A 'tone refresh' commit. The answer still reads fine, nothing returns a 500, nobody is paged. But fraud reports no longer reach a human. That is the silent regression."

### 1:00 Regress notices

See (tab 6): `ALARM [citation_correct, escalation_correct, eval_score, format_valid] -> opened inc_...; Regress is on it: <session link>` (usually during the burst).
See (console): tiles turn orange with z-scores; the new incident appears at the top.
Do: open the session link in TrueForge.
See: the runbook loads, four subagents start in parallel (what-changed, segments, impact, replay), a Daytona sandbox runs the replay harness.
Say: "Detection is arithmetic, not an LLM opinion: robust z-scores in SQL against a clean baseline. The agent then fans out four subagents and writes and runs replay code in a sandbox. It reaches four real systems over MCP: our regress server over Supabase telemetry, PostHog's own MCP for customer impact, Langfuse for prompts and traces, and GitHub for the change history and the report."
Point at (TrueForge session, impact subagent): its `posthog` `execute-sql` call counting thumbs-down and talk-to-human events, and its cross-check that PostHog agrees with Regress's numbers.

### 2:30 The evidence and the question (alarm to approval card: about 2-3 minutes)

See (console): timeline reaches `checkpointed`; the violet **Your decision** card shows `adopt-support · production v2 → v1`, blast radius, undo, and the commit that caused it.
Point at: the four gates (onset seconds after the change; replay gap about 0.5 on the same 20 inputs; excluding v2 explains every alarm; no competing change) and the report.
Do: click one number in Evidence to show its SQL.
Say: "Every number is an evidence object with the query behind it; the narrator cannot state a figure outside that set. The replay code ran in the sandbox, and the server re-scored it, so we do not have to trust the model's arithmetic. Reads and replays are free. This is the one write, and it waits for me."

### 3:15 Approve

Do: click **Approve rollback** in the console (or Allow in TrueForge).
Then immediately (tab 7), to give verification fresh traffic:
```bash
uv run python -m target.traffic --burst 30
```
See: timeline `approved → applied`; GitHub shows a new commit `Regress rollback (inc_...): adopt-support v2 -> v1`; within about a minute `verified`; a GitHub issue with the report.
Do: ask the fraud question in the bot UI: the specialist header is back, footer `prompt v1`.
Say: "Recovery is verified on fresh production traffic, never on the replay. If the harness dies between approval and apply, it reads the live label on restart and never flips twice; we killed it with kill -9 in rehearsal."

### 4:15 Second fault, opposite signature

Do (tab 7):
```bash
./scripts/fault_route.sh && sleep 7 && uv run python -m target.traffic --burst 40
```
See: burst p50 about 5 s or more (gpt-5), quality 1.00. Watcher alarms on `cost_per_request_usd` and `latency_p95_ms`, quality flat. About 3 minutes later the decision card shows `route support gpt-5 → gpt-4.1-mini`, verdict `LOCALIZED_ROUTE`, replay latency ratio about 2.5x.
Do: **Deny…**, type "we chose the bigger model on purpose; take it to capacity review", then **Deny and record**.
See: timeline `denied`; the agent's closing message names its next branch and stops; nothing changes.
Say: "Different fault, different evidence, different fix. And 'no' is a first-class answer: it records the decision and stops."

### 5:30 Close

Show the repo: the state machine (`regress_mcp/store.py`), the gates, the validator test (`tests/test_narrative.py` rejects an invented number), the approval config in `agent/bootstrap.py`.
Say: "Reads are free, replays are sandboxed, there is exactly one write per incident, it always waits for a human, prompt versions are never deleted, and a failed gate can never reach the approval step."

## 5. After the demo: reset

```bash
./scripts/revert_route.sh
uv run python -m agent.preflight
```
The route stays on gpt-5 after a Deny, and it costs 10x per request until reverted.
Wait until preflight is all PASS again before the next run.

## 6. If something goes wrong

| Symptom | Fix |
|---|---|
| Watcher says `NO BASELINE` | The machine slept or traffic stopped: the detector has no 30 minutes of clean history. Keep traffic running and wait; nothing can be detected until then. |
| Watcher stays `quiet` during the burst | Console header says "warming up": baseline too thin (too many recent faults); let clean traffic run 30 minutes. Otherwise check tab 6 for errors. |
| Watcher says "not opening a duplicate" or "only from ... retired config" | An earlier incident or a revert explains the alarms; this is by design. Revert, wait for preflight to pass, retry. |
| Allow in TrueForge does nothing | Approve from the console, or `uv run python -m agent.approve <session_id> allow`. |
| Agent says sandbox storage exhausted | Delete sandboxes in the Daytona dashboard; send the session "Resume the investigation". |
| Agent turn failed with 429 | OpenAI credits; top up, then send the session "Resume from the runbook". |
| `max clients reached` | `DATABASE_URL` must use port 6543 (transaction pooler). |
| Gate fails and verdict is NOT_LOCALIZED | That is a correct outcome; show it as "it knows when it cannot tell". |
| TrueForge cannot reach regress-mcp | TrueForge was started without `OUTBOUND_URL_ALLOWED_HOSTS`. |

## 7. Rubric, in one line each

- **Harness does the work (30):** four real systems over MCP (regress, PostHog, Langfuse, GitHub), four TrueForge subagents, agent-written replay code in the Daytona sandbox, native approval gate, session resumed after a kill.
- **It actually runs (25):** preflight all PASS on a clean machine; everything above is real traffic, real Langfuse, real commits.
- **Where it stops (20):** one gated write per incident, frozen proposal, three guards in the console, Deny path.
- **Job worth handing over (15):** "our support bot got worse after someone changed something; find it, prove it, put it back, ask me first."
- **Demo clarity (10):** two faults with opposite signatures and opposite human decisions.
