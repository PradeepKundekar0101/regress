"""Settings and static assets shared by the bot, the seeders, traffic and fault switches."""

import json
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from target.bot.scoring import GoldenItem

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / ".env")

KB_PATH = ROOT / "kb" / "faq.md"
GOLDEN_PATH = ROOT / "golden" / "golden.jsonl"
PROMPTS_DIR = ROOT / "prompts"
ROUTE_NAME = "support"

# USD per 1M tokens (input, output). Used to put a cost on every trace.
PRICING = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
}

# Reasoning models reject a temperature other than the default.
REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def supports_temperature(model: str) -> bool:
    return not model.rsplit("/", 1)[-1].startswith(REASONING_PREFIXES)


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable {name}; see .env.example")
    return value


# Supabase's transaction pooler (port 6543) multiplexes many short-lived clients; it cannot keep
# server-side prepared statements, so psycopg must not create them.
DB_KWARGS = {"prepare_threshold": None}


def db_connect(autocommit: bool = False):
    """autocommit=True for reads: no transaction stays open between queries to pin a pooler backend."""
    import psycopg
    return psycopg.connect(env("DATABASE_URL"), autocommit=autocommit, connect_timeout=15, **DB_KWARGS)


def db_pool(max_size: int, autocommit: bool = False):
    """autocommit=True for read-only pools: each query then costs one round trip instead of two (no COMMIT)."""
    from psycopg_pool import ConnectionPool
    return ConnectionPool(env("DATABASE_URL"), min_size=1, max_size=max_size,
                          kwargs={**DB_KWARGS, "autocommit": autocommit}, open=True)


def optional_env(name: str) -> str | None:
    return os.environ.get(name) or None


def posthog_project_key() -> str:
    """Capture needs the project key (phc_); a personal key (phx_) is rejected with a silent 401."""
    key = env("POSTHOG_API_KEY")
    if not key.startswith("phc_"):
        raise RuntimeError(
            "POSTHOG_API_KEY must be the project API key (phc_...), from PostHog Settings > Project > General. "
            "Put a personal key (phx_...) in POSTHOG_PERSONAL_API_KEY instead."
        )
    return key


def prompt_name() -> str:
    return env("PROMPT_NAME", "adopt-support")


def default_model() -> str:
    return env("DEFAULT_MODEL", "gpt-4.1-mini")


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float | None:
    for name in sorted(PRICING, key=len, reverse=True):
        if model == name or model.startswith(f"{name}-") or model.endswith(f"/{name}"):
            price_in, price_out = PRICING[name]
            return (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    return None


@lru_cache
def kb_text() -> str:
    return KB_PATH.read_text()


def kb_version() -> str:
    first_line = kb_text().splitlines()[0]
    return first_line.rsplit(" ", 1)[-1]  # "... knowledge base v1" -> "v1"


@lru_cache
def kb_index() -> list[dict]:
    """[{"id": "kb-01", "title": "UPI daily limit"}, ...] parsed from the "[kb-01] Title" lines."""
    entries = []
    for line in kb_text().splitlines():
        if line.startswith("[kb-"):
            entry_id, title = line[1:].split("] ", 1)
            entries.append({"id": entry_id, "title": title})
    return entries


@lru_cache
def golden_items() -> dict[str, GoldenItem]:
    items = [GoldenItem(**json.loads(line)) for line in GOLDEN_PATH.read_text().splitlines() if line]
    return {item.id: item for item in items}
