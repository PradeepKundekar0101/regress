"""Adopt.ai support bot: the production system Regress watches.

POST /reply fetches the prompt labelled `production` from Langfuse and the model from the
`support` route, answers from the FAQ knowledge base, scores the reply deterministically and
records the trace in Langfuse and one row in Postgres.
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langfuse import get_client, propagate_attributes
from openai import OpenAI, OpenAIError
from posthog import Posthog
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from target import config
from target.bot.scoring import Scores, answer_text, parse_output, score

log = logging.getLogger("adopt-bot")
STATIC = Path(__file__).parent / "static"
ROUTE_TTL_S = 5
PROMPT_TTL_S = 10

state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    state["langfuse"] = get_client()
    state["openai"] = OpenAI(
        api_key=config.env("OPENAI_API_KEY"),
        # Explicit default: with base_url=None the SDK re-reads OPENAI_BASE_URL, which may be "".
        base_url=config.optional_env("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        max_retries=1,
        timeout=60,
    )
    state["db"] = ConnectionPool(config.env("DATABASE_URL"), min_size=1, max_size=10, open=True)
    state["posthog"] = Posthog(
        config.posthog_project_key(), host=config.env("POSTHOG_HOST"),
        on_error=lambda err, batch: log.error("posthog dropped %d events: %s", len(batch), err),
    )
    state["route"] = (None, 0.0)
    yield
    state["langfuse"].flush()
    state["posthog"].shutdown()
    state["db"].close()


app = FastAPI(title="Adopt.ai support bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class ReplyRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    golden_id: str | None = None
    source: str = Field(default="ui", pattern="^(ui|traffic)$")
    session_id: str | None = None


class FeedbackRequest(BaseModel):
    trace_id: str
    kind: str = Field(pattern="^(thumbs_up|thumbs_down|talk_to_human)$")
    session_id: str | None = None
    source: str = Field(default="ui", pattern="^(ui|traffic)$")


def current_model() -> str:
    model, fetched_at = state["route"]
    if model and time.monotonic() - fetched_at < ROUTE_TTL_S:
        return model
    with state["db"].connection() as conn:
        row = conn.execute("select model from routes where name = %s", (config.ROUTE_NAME,)).fetchone()
    model = row[0] if row else config.default_model()
    state["route"] = (model, time.monotonic())
    return model


def record_request(row: dict) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join(f"%({k})s" for k in row)
    with state["db"].connection() as conn:
        conn.execute(f"insert into requests ({columns}) values ({placeholders})", row)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/kb")
def kb_index() -> list[dict]:
    return config.kb_index()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "model": current_model()}


@app.post("/reply")
def reply(req: ReplyRequest, background: BackgroundTasks):
    item = config.golden_items().get(req.golden_id) if req.golden_id else None
    if req.golden_id and item is None:
        raise HTTPException(400, f"unknown golden_id {req.golden_id}")
    session_id = req.session_id or f"{req.source}-{uuid.uuid4().hex[:12]}"
    langfuse = state["langfuse"]
    prompt = langfuse.get_prompt(config.prompt_name(), label="production", cache_ttl_seconds=PROMPT_TTL_S)
    model = current_model()
    system = prompt.compile(kb=config.kb_text())
    sampling = {"temperature": 0} if config.supports_temperature(model) else {}

    with langfuse.start_as_current_observation(name="support-reply", as_type="span", input={"question": req.question}) as root:
        with propagate_attributes(
            session_id=session_id,
            tags=[req.source] + ([item.category] if item else []),
            metadata={
                "golden_id": req.golden_id or "",
                "prompt_version": str(prompt.version),
                "route": config.ROUTE_NAME,
                "model": model,
                "kb_version": config.kb_version(),
            },
        ):
            trace_id = langfuse.get_current_trace_id()
            started = time.perf_counter()
            raw, usage, provider_error = "", None, False
            with langfuse.start_as_current_observation(
                name="llm", as_type="generation", model=model, prompt=prompt,
                model_parameters=sampling, input=[{"role": "user", "content": req.question}],
            ) as gen:
                try:
                    completion = state["openai"].chat.completions.create(
                        model=model,
                        **sampling,
                        response_format={"type": "json_object"},
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": req.question},
                        ],
                    )
                    raw = completion.choices[0].message.content or ""
                    usage = completion.usage
                    gen.update(
                        output=raw,
                        usage_details={"input": usage.prompt_tokens, "output": usage.completion_tokens},
                        cost_details=_cost_details(model, usage),
                    )
                except OpenAIError as exc:
                    provider_error = True
                    log.warning("provider error: %s", exc)
                    gen.update(level="ERROR", status_message=str(exc)[:500])
            latency_ms = int((time.perf_counter() - started) * 1000)

            scores = score(raw, item) if not provider_error else None
            root.update(output=raw)
            if scores:
                _score_trace(langfuse, scores)

    tokens_in = usage.prompt_tokens if usage else None
    tokens_out = usage.completion_tokens if usage else None
    background.add_task(record_request, {
        "trace_id": trace_id, "source": req.source, "session_id": session_id,
        "golden_id": req.golden_id, "category": item.category if item else None,
        "question": req.question, "prompt_name": config.prompt_name(), "prompt_version": prompt.version,
        "route": config.ROUTE_NAME, "model": model, "kb_version": config.kb_version(),
        "latency_ms": latency_ms, "tokens_in": tokens_in, "tokens_out": tokens_out,
        "cost_usd": config.cost_usd(model, tokens_in, tokens_out) if usage else None,
        "provider_error": provider_error, "raw_output": raw,
        **(scores.as_dict() if scores else {}),
    })
    background.add_task(langfuse.flush)
    if provider_error:
        # Return rather than raise: a raised HTTPException would drop the background writes,
        # and provider errors are one of the signals the detector watches.
        return JSONResponse(
            {"error": "model provider failed", "trace_id": trace_id}, status_code=502, background=background,
        )

    parsed = parse_output(raw) or {}
    return {
        "reply": {
            "answer": answer_text(raw, parse_output(raw)),
            "citations": parsed.get("citations") if isinstance(parsed.get("citations"), list) else [],
            "escalate": parsed.get("escalate") is True,
            "confidence": parsed.get("confidence"),
        },
        "meta": {
            "trace_id": trace_id, "session_id": session_id, "prompt_version": prompt.version,
            "model": model, "latency_ms": latency_ms, "scores": scores.as_dict() if scores else None,
        },
    }


@app.post("/feedback")
def feedback(req: FeedbackRequest) -> dict:
    if req.kind != "talk_to_human":
        state["langfuse"].create_score(
            name="user_feedback", value=1.0 if req.kind == "thumbs_up" else 0.0,
            trace_id=req.trace_id, data_type="NUMERIC",
        )
        state["langfuse"].flush()
    state["posthog"].capture(
        req.kind,
        distinct_id=req.session_id or req.trace_id,
        properties={"trace_id": req.trace_id, "source": req.source},
    )
    return {"ok": True}


def _cost_details(model: str, usage) -> dict | None:
    cost = config.cost_usd(model, usage.prompt_tokens, usage.completion_tokens)
    return {"total": cost} if cost is not None else None


def _score_trace(langfuse, scores: Scores) -> None:
    for name, value in scores.as_dict().items():
        if value is None:
            continue
        langfuse.score_current_trace(name=name, value=float(value), data_type="NUMERIC")
