# Rebuild playbook for 26 September

Build window 12:00 to 19:00 IST, demos from 19:30.
Everything below was built and run end to end in the rehearsal on 25 September; this is the order and the traps.
Read `docs/learnings.md` once before starting.

## Before 12:00 (environment only, no agent code)

- [ ] OpenAI credits topped up (a full agent run on gpt-5-6-sol is about 700k tokens, mostly cached; budget 15 runs plus traffic).
- [ ] TrueForge running with local MCP allowed:
      `OUTBOUND_URL_ALLOWED_HOSTS='["127.0.0.1","localhost"]' npx @truefoundry/trueforge`
- [ ] TrueForge Settings: OpenAI model provider added; Daytona sandbox provider ready with auto-delete about 60 minutes (the 7200-minute default filled the storage quota and blocked the agent).
- [ ] Daytona dashboard: old sandboxes deleted.
- [ ] Langfuse connector registered through the API, not the form (the form turns `Basic` into `Bearer Basic`):
      `PUT /api/v1/settings/mcp-servers` with `Authorization: Basic base64(pk:sk)`.
- [ ] GitHub connector added in TrueForge (catalog, fine-grained PAT with Issues read/write on the config repo).
- [ ] `.env` filled, no quotes around values. `DATABASE_URL` uses the Supabase transaction pooler (port 6543). PostHog: project key `phc_` for capture, personal key `phx_` plus project id for reads.
- [ ] Gateway credentials (10:30): set `OPENAI_BASE_URL` and the key in `.env` if using the TrueFoundry AI Gateway.

## 12:00 to 13:30: target system (rehearsal took about 3 hours; now about 90 minutes)

1. `uv init`, deps: fastapi uvicorn openai langfuse psycopg[binary] psycopg-pool posthog httpx python-dotenv pydantic mcp[cli]; dev pytest.
2. KB (20 entries with `[kb-NN] Title`), golden set (40, with escalate/refusal/citations/must_contain), baseline and regressed prompts.
   The regressed prompt is a "tone refresh" that drops the escalation block and the JSON schema but keeps the word JSON (JSON mode needs it).
3. Scorer first, tests first: four checks, mean is eval_score.
4. Bot: prompt by `production` label (10 s cache), model from the `routes` table, one shared `llm.generate`, background write of one row, 502 returned (not raised) on provider error.
5. Seed script (schema, golden, route, two prompt versions with `config.variant`), fault switches committing to the config repo, traffic with `--burst`.
6. Check: burst 40 on v1 scores about 0.99; trip the prompt fault, burst, see about 0.5; revert.
   Start background traffic now so the detector has 2 hours of baseline by the demo.
   Do not trip faults in the 2 hours before the demo except rehearsed ones that open incidents (their periods are masked); the detector needs 30 minutes of clean history.

## 13:30 to 15:30: regress-mcp

1. SQLite store and state machine with tests (cannot skip to applied; failed gates cannot reach approval).
2. Detector as one SQL query: robust z, MAD floor, volume, effect size. `exclude` and `only` filter the current window only, never the baseline.
3. Localize by exclusion, with coverage when one segment is the whole window.
4. Replay through the production `llm.generate`; `replay_generate` returns a summary; `get_replay_outputs` is read-only for code mode; `verify_report` re-scores.
5. Gates and proposal, narrative validator (reject bare numbers and any leftover `{{`), gated actions with live-state reconciliation, `verify_recovery` on the restored version only.
6. Duplicate-incident suppression in `run_detector`.
7. Raise `ToolError` for refusals (MCP 2.x hides other exception text).
8. Check: drive one full incident over the MCP client before touching TrueForge.

## 15:30 to 16:30: agent on TrueForge

1. Public skill repo; `publish_skill.sh` prints the SHA; skill manifest needs a description; no preload for git skills.
2. `bootstrap.py`: regress connector, skill, agent with `require_approval_for_tools: [rollback_execute, route_revert]`, Langfuse read-only list, GitHub limited to commits and issues.
3. Watcher and `tail_session.py` (paginate events; follow across approval turns).
4. Check: one prompt-fault run to the approval card.

## 16:00: mentor checkpoint

Show the approval card, the gates, the refused tampered replay report and the refused invented number.

## 16:30 to 18:00: second fault, console, polish, README

- Incident console (`console/`): approvals through the TrueForge API with the three guards; about 45 minutes with the design already settled.

- Route fault to gpt-5 (quality flat, cost and p95 alarm): LOCALIZED_ROUTE, deny it on stage.
- README: pitch, architecture, safety policy (reads free, one gated write, deletes never, failed gate cannot reach approval), how to run, AI-assistance disclosure, prior art (ONCALL, RootCauseOS).

## 18:00 to 19:00: clean-clone test and recording

- Clone on another machine, follow the README only.
- Record: prompt fault, detector, subagents, approval card, Allow, recovery; route fault, Deny; show the state table and the tests.

## Demo script (about 5 minutes)

| Time | Beat |
|---|---|
| 0:00 | Ada answering in the UI; baseline green. |
| 0:30 | `./scripts/fault_prompt.sh` then `--burst 50`: the fraud answer loses its specialist handoff. |
| 1:00 | Watcher alarm, session opens, four subagents in parallel, sandbox replay. |
| 2:15 | Validated report and the approval card for `rollback_execute`. |
| 2:45 | Allow. Label flips, commit appears, verify on fresh traffic, GitHub issue. |
| 3:30 | `./scripts/fault_route.sh`: cost and p95 alarm, LOCALIZED_ROUTE, Deny; the agent records it and stops. |
| 4:30 | Repo tour: state machine, gates, validator test, policy. |

Leave 5 minutes between the two faults, or say on stage that the overlapping-window case is handled (gate 3 attributes alarms to the rolled-back version).
If the UI Allow button does not respond, `uv run python -m agent.approve <session> allow`.
