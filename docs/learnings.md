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
