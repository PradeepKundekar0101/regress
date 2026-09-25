"""One-time setup: schema, golden set, default route, and the two prompt versions in Langfuse.

Idempotent. Run with `uv run python -m target.seed`.
"""

import psycopg
from langfuse import get_client

from target import config, config_repo
from target.prompts_registry import find_version, production_version

SCHEMA = config.ROOT / "sql" / "001_schema.sql"


def seed_db() -> None:
    with config.db_connect() as conn:
        conn.execute(SCHEMA.read_text())
        for item in config.golden_items().values():
            conn.execute(
                """insert into golden_set values (%s, %s, %s, %s, %s, %s, %s)
                   on conflict (id) do update set category = excluded.category,
                     question = excluded.question, expect_escalate = excluded.expect_escalate,
                     expect_refusal = excluded.expect_refusal,
                     expect_citations = excluded.expect_citations, must_contain = excluded.must_contain""",
                (item.id, item.category, item.question, item.expect_escalate,
                 item.expect_refusal, item.expect_citations, item.must_contain),
            )
        conn.execute(
            "insert into routes (name, model, updated_by) values (%s, %s, 'seed') on conflict do nothing",
            (config.ROUTE_NAME, config.default_model()),
        )
    print(f"db: schema applied, {len(config.golden_items())} golden items, route '{config.ROUTE_NAME}' present")


def seed_prompts() -> None:
    langfuse = get_client()
    name = config.prompt_name()
    for variant, labels in (("baseline", ["production"]), ("regressed", [])):
        existing = find_version(langfuse, name, variant)
        if existing:
            print(f"langfuse: {name} {variant} already exists as v{existing}")
            continue
        text = (config.PROMPTS_DIR / f"{variant}.txt").read_text()
        created = langfuse.create_prompt(
            name=name, prompt=text, labels=labels, type="text",
            config={"variant": variant},
            commit_message="Initial support prompt" if variant == "baseline" else "Tone refresh: warmer, shorter instructions",
        )
        print(f"langfuse: created {name} v{created.version} ({variant}) labels={labels}")
    langfuse.flush()


def seed_config_repo() -> None:
    if not config_repo.enabled():
        print("config repo: CONFIG_REPO not set, skipping")
        return
    name = config.prompt_name()
    version = production_version(get_client(), name)
    with config.db_connect() as conn:
        model = conn.execute("select model from routes where name = %s", (config.ROUTE_NAME,)).fetchone()[0]
    shas = [
        config_repo.record_prompt(name, version, f"Record current production prompt ({name} v{version})"),
        config_repo.record_route(config.ROUTE_NAME, model, f"Record current support route ({model})"),
    ]
    print(f"config repo: prompt v{version}, route {model}, commits {[s[:7] if s else 'unchanged' for s in shas]}")


if __name__ == "__main__":
    seed_db()
    seed_prompts()
    seed_config_repo()
