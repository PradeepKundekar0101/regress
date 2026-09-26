# Customer-view video: before vs after

Status: planned, not implemented.
Stretch goal for the 16:30 to 18:00 block; build it only after both demo beats (prompt approve, route deny) pass end to end.

## Goal

Show the human approver, and the demo audience, what a customer actually sees, not only numbers.
Record the real Ada chat UI in production answering the same fraud question twice:

- **Before**: while the regression is live (prompt v2), captured when the incident reaches `checkpointed`, before anyone approves. Expected: a warm answer with no "Passed to a specialist" banner and no `kb-10` source.
- **After**: once recovery is verified (prompt v1). Expected: the amber specialist banner and the `kb-10` source are back.

The console plays both clips side by side, in sync, labelled with prompt version and model.
The decision card shows "Before" while the human decides; the decided card shows both.

## Design

### 1. Recorder: deterministic Playwright, not an LLM browser

New module `regress_mcp/customer_view.py`.

- Dependency: `uv add playwright` then `uv run playwright install chromium`.
- Headless Chromium, `new_context(viewport={"width": 900, "height": 640}, record_video_dir=<tmp>, record_video_size={"width": 900, "height": 640})`.
- Script: open `BOT_URL` with `?probe=1`, type the golden fraud question (`g18`: "There is a charge on my card I did not make.") into `#q`, press Enter, wait for the new `.slip` element (timeout 45 s), wait 2 s so the viewer can read it, close the context (Playwright finalises the video on close), move the file to its final path.
- Also save a PNG screenshot of the reply slip.
- Read back what the page showed and return it: whether `.slip .handoff` is present, the `kb-` citation ids in `.ledger`, and the footer text (`prompt vN · model · ms`). These become evidence, so the claim "the banner is back" is checked, not just filmed.
- Transcode to MP4 with ffmpeg if available (`ffmpeg -y -i in.webm -c:v libx264 -pix_fmt yuv420p out.mp4`): Safari does not play WebM reliably. Keep the WebM if ffmpeg is missing.
- Selectors to verify against `target/bot/static/index.html` first (another session redesigned parts of the UI): input `#q`, reply `.slip`, banner `.slip .handoff`, citations `.ledger`, footer `.meta`.

### 2. Keep the probe out of the statistics

The recording sends one real request through the bot. It must not move the numbers it is proving.

- `target/bot/app.py`: allow `source="probe"` (the `ReplyRequest` and `FeedbackRequest` patterns are `^(ui|traffic)$` today).
- `target/bot/static/index.html`: when the page URL has `?probe=1`, send `source: "probe"` in `/reply`.
- Exclude `source = 'probe'` everywhere statistics are computed: the detector's `scoped` CTE in `regress_mcp/detector.py` (the `where r.ts >= p.as_of - p.w - p.b` clause), `window_stats` and `traces` in `regress_mcp/sources.py`, the share queries in `regress_mcp/localize.py`, and the console's `SERIES_SQL` in `console/app.py`.
- Probe requests have no `golden_id`, so quality signals already ignore them, but latency, cost and provider-error signals count all traffic, so the exclusion is required.

### 3. MCP tool

In `regress_mcp/server.py`, add `capture_customer_view(incident_id: str, phase: str)` with `phase` in `before | after`, using the existing `@tool(ANALYSE)` decorator (it writes files and evidence, so it is not read-only; code mode will refuse it, so the agent calls it directly).

- Runs on the host, where `localhost:8000` is reachable (the Daytona sandbox cannot reach localhost).
- Refuses `before` unless the incident is `checkpointed` and `after` unless it is `verified`; raise `ValueError` so the agent reads the reason.
- Saves to `.regress/media/<incident_id>/<phase>.mp4` (or `.webm`) and `<phase>.png`.
- Adds evidence rows via `store.add_evidence` (see `regress_mcp/store.py`): one per observed fact, for example label `customer view before: specialist banner shown`, value 0 or 1, unit `count`, source `{"kind": "video", "path": ..., "prompt_version": ..., "model": ..., "question": ...}`, computed_by `regress-mcp/customer_view`.
- Returns the paths, the observed banner and citations, and the evidence ids.
- Never blocks the incident: on any failure (browser, timeout) return `{"captured": false, "reason": ...}` and do not transition state. Video is decoration, never a gate.
- Takes about 15 s; well inside the MCP timeout, but keep the Playwright timeouts explicit.

### 4. Agent and runbook

In `skills/regress-runbook/SKILL.md`:

- After `check_gates` returns `checkpointed` and before calling the gated tool: `capture_customer_view(incident_id, "before")`.
- After `verify_recovery` returns `verified`: `capture_customer_view(incident_id, "after")`.
- In the report, under a "Customer view" line: cite the banner and citation evidence for before and after; if a capture failed, say so in one line and continue.

Publish with `uv run python -m agent.bootstrap` (it republishes the skill to the public skill repo and pins the new commit).

### 5. Console

In `console/app.py`:

- `GET /api/incidents/{incident_id}/media/{phase}` serving the file with the right content type (`video/mp4` or `video/webm`); `404` when absent. Validate `phase` against `before | after` and never build paths from unchecked input.
- Include `media: {"before": {...}, "after": {...}}` (url, prompt version, banner shown, citations) in the incident detail response.

In `console/static/index.html` (coordinate: another session owns this file's redesign):

- A "What customers see" panel: two `<video muted playsinline preload="metadata">` elements side by side (stacked on phones), each labelled "Before · prompt v2 · no specialist" / "After · prompt v1 · specialist banner".
- One play/pause control that starts both from 0 together, so the difference is watched at once.
- On the decision card: "Before" only, with the "After" slot saying "recorded after the fix is verified".
- Respect `prefers-reduced-motion`: no autoplay.

### 6. Optional: one shareable clip

`ffmpeg -i before.mp4 -i after.mp4 -filter_complex "[0:v]drawtext=text='Before (v2)':x=16:y=16:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.5[a];[1:v]drawtext=text='After (v1)':x=16:y=16:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.5[b];[a][b]hstack" side_by_side.mp4`.
Serve it from the console as a download for the demo video and the build-story post.

## Tests

- Unit: `customer_view` parses a saved HTML fixture of a reply slip (banner present and absent) into the observed facts; the tool refuses the wrong phase for the incident's status; the media endpoint rejects unknown phases and path tricks.
- Unit: detector and `window_stats` ignore `source = 'probe'` rows (use a query-string check or a small SQL fixture).
- Live: trip the prompt fault, let the agent reach `checkpointed`, confirm `before.mp4` shows no banner and its evidence says so; approve, confirm `after.mp4` shows the banner; play both in the console in Chrome and Safari.
- Check that running the probe during an incident does not change the detector's numbers for that window.

## Acceptance

- Before and after clips exist for a real incident and play in sync in the console.
- The report cites evidence for "banner absent before, present after", taken from the page, not asserted.
- Probe traffic never appears in detector, localisation or console statistics.
- A capture failure leaves the incident flow unaffected.
- All existing tests still pass.

## Prompt for the implementing session

```
You are working in /Users/pradeepkundekar/Desktop/Projects/regress-v0, the Regress project: an on-call agent
for LLM apps built on TrueForge. Read docs/customer-view-video-plan.md fully, then docs/learnings.md and
docs/demo-runbook.md, then implement the plan exactly, in this order:

1. Probe traffic first: allow source="probe" in target/bot/app.py, send it from the bot UI when the URL has
   ?probe=1, and exclude source='probe' from every statistic (detector.py scoped CTE, sources.py window_stats
   and traces, localize.py share queries, console/app.py SERIES_SQL). Add a test.
2. regress_mcp/customer_view.py: deterministic Playwright recording of the fraud question (golden g18) against
   BOT_URL?probe=1, returning the observed banner, citations and footer. Verify the selectors against
   target/bot/static/index.html before relying on them. Transcode to MP4 with ffmpeg when available.
3. capture_customer_view(incident_id, phase) in regress_mcp/server.py with the existing @tool(ANALYSE)
   decorator: phase guards, evidence rows via store.add_evidence, files under .regress/media/<incident>/, and
   {"captured": false, "reason": ...} on any failure. It must never change incident state or block the flow.
4. Runbook: add the two capture steps and a "Customer view" report line in skills/regress-runbook/SKILL.md,
   then run `uv run python -m agent.bootstrap` to republish the skill and update the agent.
5. Console: media endpoint and detail fields in console/app.py, and the synced side-by-side player in
   console/static/index.html. Another session may be editing the console UI: check `git status` and recent
   commits first, keep its structure and styles, and make the smallest change that adds the panel.

Rules:
- Do not restart or kill services on :8000, :8100, :8941 or :8790 without asking me first; tell me which
  process needs a restart to pick up a change.
- Commit only the files you changed, with a clear message; never commit .env or .regress/.
- Run `uv run pytest -q` before each commit. Follow ~/.claude/CLAUDE.md (no em dashes, no agent co-author).
- Verify live at the end: trip ./scripts/fault_prompt.sh with a --burst 50, wait for the incident to reach
  checkpointed, confirm before.mp4 shows no specialist banner and its evidence says so, approve from the
  console, run `uv run python -m target.traffic --burst 30`, confirm after.mp4 shows the banner, and play both
  in the console. Then revert with ./scripts/revert_prompt.sh and run `uv run python -m agent.preflight`.
- Only trip faults when preflight is all PASS (the detector needs 30 minutes of clean history).
```
