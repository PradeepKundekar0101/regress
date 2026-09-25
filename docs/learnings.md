# Rehearsal learnings (2026-09-25)

Things that cost time today and must not cost time tomorrow.

## Environment

- TrueForge 0.2.1 connector form prepends `Bearer ` to any `Authorization` value, so Langfuse MCP (Basic auth) returns 401.
  Register it through the API instead: `PUT http://localhost:8790/api/v1/settings/mcp-servers` with `auth.headers.Authorization = "Basic <base64(pk:sk)>"`, then check `GET /api/v1/mcp-servers/langfuse/tools` lists ~86 tools.
- The Langfuse MCP exposes write and delete tools (`updatePromptLabels`, `delete*`).
  Attach it to the agent with a read-only `enable_tools` list so the only write path stays the gated `rollback_execute`.
- New Langfuse Cloud orgs (created after 2026-09-16) cannot use the legacy APIs: `GET /api/public/traces/{id}` and `/v2/scores` return 410.
  Use `GET /api/public/v2/observations?fromStartTime=&toStartTime=&traceId=&fields=core,basic,usage,prompt,model,metadata` and `GET /api/public/v3/scores?traceId=`.
  Non-OTel public APIs may lag several minutes, so the detector reads Supabase, not Langfuse.
- Do not quote values in `.env`: `docker run --env-file` keeps the quotes literally (python-dotenv and compose strip them).
- An empty `OPENAI_BASE_URL=` breaks the OpenAI SDK even when `base_url=None` is passed, because the SDK re-reads the env var. Pass an explicit default.
- Check the OpenAI account has credits before anything else (`429 insufficient_quota` looks like a code bug at first).
- PostHog capture needs the project key (`phc_`); a personal key (`phx_`) gets a 401 that the SDK swallows. Reading events back (HogQL) needs the personal key and the numeric project id.
- TrueForge refuses MCP servers on private hosts ("Outbound URL blocked for host 127.0.0.1"). Start it with `OUTBOUND_URL_ALLOWED_HOSTS='["127.0.0.1","localhost"]'`.
- TrueForge needs a model provider (Settings > Models) before any agent runs; check `GET /api/v1/models` is non-empty.
- MCP Python SDK 2.x renamed FastMCP to `MCPServer` (`from mcp.server.mcpserver import MCPServer`); Python attributes are snake_case (`destructive_hint`) but the wire format stays camelCase.
- MCP 2.x hides the text of unexpected exceptions; raise `ToolError` for refusals the agent must read.
- Code mode is on whenever the sandbox is enabled: sandbox Python calls `await call_tool(server, tool, body={...})`, only printed output reaches the context, and approval gates still apply.

## Bot and telemetry

- FastAPI drops `BackgroundTasks` when a handler raises `HTTPException`; return a `JSONResponse(..., background=...)` for 502s or provider errors vanish from telemetry.
- The Langfuse SDK prompt cache is stale-while-revalidate: for one batch after a label flip, some requests still run the old version.
  Verification after a rollback must filter to requests on the new version (or wait out the TTL), or it will report a false `verify_failed`.
- Serve the UI with `Cache-Control: no-store` so a mid-demo reload shows the current page.

## Measured fault signatures (real numbers from today)

| Fault | eval | format valid | escalation correct (fraud, disputes) | p50 latency | cost/request |
|---|---|---|---|---|---|
| Baseline v1 on gpt-4.1-mini | 0.99-1.00 | 100% | 100% | ~1.7 s | $0.00059 |
| Prompt v2 "tone refresh" | 0.47-0.60 | 0-25% | 0% | ~2.0 s (flat) | $0.00052 (flat) |
| Route to gpt-4.1 | 0.99 | 100% | 100% | ~1.4 s (not slower) | $0.00299 (5x) |
| Route to gpt-5 | 1.00 | 100% | 100% | ~11 s (6.5x), p95 28 s | $0.00587 (10x) |

- v2 keeps the answer text correct (content_ok 100%) but renames `citations` to `kb_ids` and drops `escalate`: a silent regression a human skimming answers would miss.
- gpt-4.1 is not slower than gpt-4.1-mini, so the route fault uses gpt-5 (a reasoning model; it rejects `temperature`).

## Detector and gates (measured on real faults)

- The prompt fault alarms on exactly the four quality signals (format valid z about -17, citations -17, eval -10, escalation -4.5); excluding `prompt_version=2` clears every alarm.
- Two faults less than 5 minutes apart share the detector window: the route incident sees the prompt fault's quality alarms too.
  Localisation separates them (`prompt_version=2` explains quality, `model=gpt-5` explains latency and cost), and gate 3 accepts alarms explained by a segment that is no longer live.
  The plan's demo timing (route fault at 3:45, about 30 s after the prompt rollback) hits exactly this case.
- A baseline eval score of about 1.0 has MAD 0; without a MAD floor every wobble would alarm.
- Excluding the only segment in a window leaves no volume and would falsely "explain" every alarm; count an alarm as explained only when the exclusion run still has volume.
- The narrative validator must reject leftover `{{...}}`, not only unknown ids, or a malformed placeholder slips through unrendered.
- End-to-end on the prompt fault: onset 44 s after the change, replay gap 0.51 on 20 traces, all four gates passed, rollback applied once and a second call returned `already_applied`, recovery verified on fresh v1 traffic.
