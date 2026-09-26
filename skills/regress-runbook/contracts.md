# Subagent contracts

Each subagent's final message must be a single JSON object and nothing else: first character `{`, last character `}`, no code fences.
Every number inside `findings` must also appear as an evidence id in `evidence_ids`; say "see ev_x" instead of restating figures.
Use `status: "insufficient"` with `unknowns` filled in when the tools could not answer.

Common shape:

```json
{
  "contract_version": 1,
  "role": "<role>",
  "incident_id": "<incident id>",
  "status": "complete | insufficient",
  "findings": {},
  "evidence_ids": ["ev_..."],
  "unknowns": ["..."]
}
```

## what_changed

Tools: `regress.get_changes`, `regress.get_prompt`, `regress.get_route`.
Find every prompt label move and route change in the last 60 minutes with its commit message, and diff the suspect prompt version against the previous one.

```json
"findings": {
  "changes": [{"change_id": 0, "ts": "", "kind": "prompt|route", "from": "", "to": "", "commit_message": "", "by_regress": false}],
  "prompt_diff_summary": "which instruction blocks were removed or reworded",
  "live": {"prompt_version": 0, "model": ""}
}
```

## segments

Tools: `regress.localize`, `regress.get_traces`.
Say which change segment explains which alarm, and which customer categories degraded most.

```json
"findings": {
  "candidates": [{"dimension": "prompt_version|model", "value": "", "explains": [], "still_alarming_without_it": []}],
  "unexplained": [],
  "worst_categories": [{"category": "", "evidence": "ev_..."}],
  "example_trace_ids": []
}
```

## impact

Tools: `regress.get_window_stats` (group_by `model` and `prompt_version`), `regress.get_user_signals`, and the read-only `posthog` connector (`execute-sql` runs HogQL over the `events` table).
Quantify latency, cost and customer impact, and list what did not move (the ruled-out list).
Cross-check the customer impact at the source: run your own HogQL in PostHog for `thumbs_down` and `talk_to_human` in the incident window (event property `trace_id` links each event to a bot trace) and report whether PostHog agrees with `regress.get_user_signals`.
Figures you report still cite the regress evidence ids; PostHog is the independent check and the drill-down (which questions and sessions complained).

```json
"findings": {
  "moved": [{"signal": "", "evidence": "ev_..."}],
  "unchanged": [{"signal": "", "evidence": "ev_..."}],
  "user_signals": {"thumbs_down_current": "ev_...", "talk_to_human_current": "ev_...", "available": true},
  "posthog_cross_check": {"agrees": true, "hogql": "the query you ran", "note": "counts match / differ because ..."}
}
```

## replay

Tools: `regress.get_changes`, `regress.get_traces`, `regress.replay_generate`, `regress.submit_replay_report`, and the sandbox (code mode can call only read-only tools such as `regress.get_replay_outputs`).
The parent gives you the suspect segment from `localize` (for example prompt_version=2). The suspect arm uses that value; the baseline arm uses the `from` value of the change that introduced it (the change whose `to` is the suspect), not simply the newest change. Keep the other dimension at the value it had in the incident window.
Call `replay_generate` directly (20 inputs; 10 if either arm is a gpt-5 or o-series reasoning model).
Then adapt `replay_harness.py` from this skill (set REPLAY_ID) and run it in the sandbox: it fetches the outputs, scores them and prints `REPORT_JSON {...}`.
Submit that JSON with a direct `submit_replay_report` call; code mode refuses tools that write state.
The server re-scores the same outputs; if it rejects your numbers, fix the scorer to match the rules in the harness, do not edit numbers by hand.

```json
"findings": {
  "replay_id": "rp_...",
  "baseline": {"prompt_version": 0, "model": ""},
  "suspect": {"prompt_version": 0, "model": ""},
  "verified": true,
  "coverage": "n/m",
  "eval_gap": "ev_...",
  "latency_ratio": "ev_...",
  "cost_ratio": "ev_..."
}
```
