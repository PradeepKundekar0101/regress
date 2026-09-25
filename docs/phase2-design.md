# Phase 2 design: regress-mcp

One Python MCP server (official `mcp` SDK, FastMCP, streamable HTTP on `:8941`), registered in TrueForge through the settings API.
The arithmetic and the single write live here, deterministic and testable.
The judgement and the replay code belong to the agent.

## Tools

Reads, free:

| Tool | Returns | Source |
|---|---|---|
| `get_window_stats(minutes, group_by?)` | every signal per window, optionally by `prompt_version`, `model` or `category` | Supabase SQL |
| `get_changes(since, until)` | prompt label moves, route changes and config commits with SHA and timestamp | Langfuse, GitHub API, `change_log` |
| `get_traces(filter, limit)` | trace IDs with inputs and golden IDs, used as replay inputs | Supabase |
| `get_user_signals(minutes)` | thumbs-down and talk-to-human rates | PostHog HogQL |
| `get_route()`, `get_prompt(version)` | live model; prompt text and config | Supabase, Langfuse |

Deterministic analysis: `run_detector`, `localize`, `replay_generate`, `check_gates`, `validate_narrative`.

Writes: `rollback_execute` and `route_revert`, both gated with `require_approval_for_tools`; `record_decision` for denials and conflicts.
The Langfuse MCP connector is attached to the agent with a read-only `enable_tools` list.

## Detector

SQL over `requests` in 5-minute buckets; the current window is compared with the previous 2 hours.
Signals: eval score, format valid, escalation correct, citation correct, refusal rate and provider-error rate from golden traffic, plus p50 and p95 latency and cost per request from all traffic.
Robust z is `0.6745 * (x - median) / MAD`, with a MAD floor of 0.02 for rates and 5% of the median for latency and cost, because a near-constant baseline would otherwise give an infinite z.
An alarm needs |z| > 3.5, at least 20 requests in the window, and an effect of at least 10 percentage points on a rate or at least 2x on latency or cost.
Onset is the first bucket in alarm.

## Evidence and narration

Every number a tool returns is an evidence row: `{id, label, value, unit, window, source: {kind, query | trace_ids | url}, computed_by}`.
The narrator writes numbers only as `{{ev_N}}` placeholders.
The validator substitutes them and rejects text with an unresolved placeholder or any other digit; the narrator gets one retry, then a deterministic template is used.
A unit test feeds a hallucinated number and asserts rejection.

## Replay

The sandbox holds no credentials, so `replay_generate` makes the model calls and stores the raw outputs.
The agent writes the replay harness in the Daytona sandbox from the runbook template: it loads the outputs, scores them against golden expectations, computes the version gap and emits a report.
`check_gates` re-scores the stored outputs deterministically and rejects the report if the agent's numbers disagree.

## Gates and verdicts

1. Onset is within 10 minutes of the candidate change.
2. Replay reproduces a gap of at least 0.15 in eval score or at least 2x in latency.
3. Excluding the candidate segment removes the sibling alarms.
4. No second candidate change exists in the window.

Verdicts: `LOCALIZED_PROMPT`, `LOCALIZED_ROUTE`, optional `LOCALIZED_RETRIEVAL`, and the legitimate endings `NOT_LOCALIZED` and `INSUFFICIENT_DATA`.
Every report carries a ruled-out list with numbers.

## State machine and the gated write

SQLite at `.regress/state.sqlite` with `incidents`, `transitions` and `evidence`; every transition writes a row with its evidence IDs.

```
detected -> planned -> replayed -> not_localized
                                -> checkpointed (gates passed, proposal frozen)
                                     -> approved -> applied -> verified | verify_failed
                                     -> denied | conflict
```

`rollback_execute(incident_id, prompt, label, from_version, to_version)` runs only from `checkpointed`, and its arguments must equal the frozen proposal, so the approval card shows exactly what changes.
On restart it reads the live label: still `from` means apply, already `to` means mark applied without flipping, anything else is `conflict`.
Each flip is also committed to the config repo; `route_revert` follows the same pattern.
`verify_recovery` counts only requests on the new version after `applied_at`, because the Langfuse prompt cache serves the old version briefly.
Hard limits: allowlisted prompt and route only, prompt versions are never deleted, and a failed gate can never reach `checkpointed`.

## Build order

1. Spike how outputs reach the sandbox: TrueForge code mode, or large-tool-response offload to files.
2. Server skeleton with read tools and the evidence store, registered in TrueForge.
3. Detector and localisation SQL, tested against today's real fault windows.
4. Replay, gates and validator with unit tests.
5. State machine, gated tools and restart reconciliation.
