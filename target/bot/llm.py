"""The single model call used by the bot and by Regress replays, so a replay runs exactly what production runs."""

import time
from dataclasses import dataclass

from openai import OpenAI

from target import config


@dataclass(frozen=True)
class Generation:
    raw: str
    latency_ms: int
    tokens_in: int
    tokens_out: int
    cost_usd: float | None


def make_client(max_retries: int = 1) -> OpenAI:
    return OpenAI(
        api_key=config.env("OPENAI_API_KEY"),
        # Explicit default: with base_url=None the SDK re-reads OPENAI_BASE_URL, which may be "".
        base_url=config.optional_env("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        max_retries=max_retries,
        timeout=90,
    )


def sampling_params(model: str) -> dict:
    return {"temperature": 0} if config.supports_temperature(model) else {}


def generate(client: OpenAI, model: str, system: str, question: str) -> Generation:
    """One support reply. Raises openai.OpenAIError on provider failure."""
    started = time.perf_counter()
    completion = client.chat.completions.create(
        model=model,
        **sampling_params(model),
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": question}],
    )
    usage = completion.usage
    return Generation(
        raw=completion.choices[0].message.content or "",
        latency_ms=int((time.perf_counter() - started) * 1000),
        tokens_in=usage.prompt_tokens,
        tokens_out=usage.completion_tokens,
        cost_usd=config.cost_usd(model, usage.prompt_tokens, usage.completion_tokens),
    )
