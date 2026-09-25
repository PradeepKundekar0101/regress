-- Telemetry for the Adopt.ai support bot. Idempotent: safe to re-run.

create table if not exists golden_set (
  id               text primary key,
  category         text not null,
  question         text not null,
  expect_escalate  boolean not null,
  expect_refusal   boolean not null,
  expect_citations text[] not null,
  must_contain     text[] not null
);

-- The model behind the bot. Stands in for the AI Gateway route until gateway credentials exist.
create table if not exists routes (
  name        text primary key,
  model       text not null,
  updated_at  timestamptz not null default now(),
  updated_by  text not null
);

-- One row per /reply call. Every figure the detector reports is a query over this table.
create table if not exists requests (
  id                  bigserial primary key,
  ts                  timestamptz not null default now(),
  trace_id            text not null,
  source              text not null,          -- traffic | ui
  session_id          text,
  golden_id           text references golden_set(id),
  category            text,
  question            text not null,
  prompt_name         text not null,
  prompt_version      integer not null,
  route               text not null,
  model               text not null,
  kb_version          text not null,
  latency_ms          integer not null,
  tokens_in           integer,
  tokens_out          integer,
  cost_usd            numeric(12, 8),
  provider_error      boolean not null,
  format_valid        boolean,
  escalated           boolean,
  citation_present    boolean,
  refusal             boolean,
  escalation_correct  boolean,
  citation_correct    boolean,
  content_ok          boolean,
  eval_score          real,
  raw_output          text
);
create index if not exists requests_ts_idx on requests (ts);

-- Every change to what the bot runs: prompt label moves, route changes, KB swaps.
create table if not exists change_log (
  id          bigserial primary key,
  ts          timestamptz not null default now(),
  kind        text not null,                  -- prompt | route | kb
  target      text not null,
  from_value  text,
  to_value    text not null,
  actor       text not null,
  commit_sha  text,
  note        text
);
create index if not exists change_log_ts_idx on change_log (ts);
