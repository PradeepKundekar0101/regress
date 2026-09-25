"""Fault switches for the demo. Each one makes a real change to what the bot runs and logs it.

    uv run python -m target.faults prompt break     # production label -> regressed prompt
    uv run python -m target.faults prompt restore   # production label -> baseline prompt
    uv run python -m target.faults route break      # support route -> larger model
    uv run python -m target.faults route restore    # support route -> default model
    uv run python -m target.faults status
"""

import argparse
import getpass

import psycopg
from langfuse import get_client

from target import config, config_repo
from target.prompts_registry import find_version, production_version

LARGE_MODEL_DEFAULT = "gpt-5"

# Commit messages read like ordinary changes: the regression is not announced in the history.
PROMPT_MESSAGES = {
    "break": "Tone refresh: warmer, shorter support prompt",
    "restore": "Restore previous support prompt",
}
ROUTE_MESSAGES = {
    "break": "Upgrade support route to {model} for better answers",
    "restore": "Move support route back to {model}",
}


def log_change(conn, kind: str, target: str, from_value: str, to_value: str, note: str, commit_sha: str | None) -> None:
    conn.execute(
        """insert into change_log (kind, target, from_value, to_value, actor, commit_sha, note)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (kind, target, from_value, to_value, f"{getpass.getuser()} (fault switch)", commit_sha, note),
    )


def switch_prompt(action: str) -> None:
    langfuse = get_client()
    name = config.prompt_name()
    variant = "regressed" if action == "break" else "baseline"
    target_version = find_version(langfuse, name, variant)
    if target_version is None:
        raise SystemExit(f"no {variant} version of {name}; run `python -m target.seed` first")
    current = production_version(langfuse, name)
    if current == target_version:
        print(f"prompt: production already on v{current} ({variant})")
        return
    langfuse.update_prompt(name=name, version=target_version, new_labels=["production"])
    sha = config_repo.record_prompt(name, target_version, f"{PROMPT_MESSAGES[action]} ({name} v{target_version})")
    with psycopg.connect(config.env("DATABASE_URL")) as conn:
        log_change(conn, "prompt", f"{name}:production", f"v{current}", f"v{target_version}", f"prompt {action}", sha)
    print(f"prompt: production v{current} -> v{target_version} ({variant})" + (f"  commit {sha[:7]}" if sha else ""))


def switch_route(action: str) -> None:
    to_model = config.env("LARGE_MODEL", LARGE_MODEL_DEFAULT) if action == "break" else config.default_model()
    with psycopg.connect(config.env("DATABASE_URL")) as conn:
        current = conn.execute("select model from routes where name = %s", (config.ROUTE_NAME,)).fetchone()[0]
        if current == to_model:
            print(f"route: {config.ROUTE_NAME} already on {to_model}")
            return
        conn.execute(
            "update routes set model = %s, updated_at = now(), updated_by = %s where name = %s",
            (to_model, "fault switch", config.ROUTE_NAME),
        )
        sha = config_repo.record_route(config.ROUTE_NAME, to_model, ROUTE_MESSAGES[action].format(model=to_model))
        log_change(conn, "route", config.ROUTE_NAME, current, to_model, f"route {action}", sha)
    print(f"route: {config.ROUTE_NAME} {current} -> {to_model}" + (f"  commit {sha[:7]}" if sha else ""))


def status() -> None:
    langfuse = get_client()
    name = config.prompt_name()
    with psycopg.connect(config.env("DATABASE_URL")) as conn:
        model = conn.execute("select model from routes where name = %s", (config.ROUTE_NAME,)).fetchone()[0]
    print(f"prompt {name}: production=v{production_version(langfuse, name)} "
          f"baseline=v{find_version(langfuse, name, 'baseline')} regressed=v{find_version(langfuse, name, 'regressed')}")
    print(f"route {config.ROUTE_NAME}: {model}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("switch", choices=["prompt", "route", "status"])
    parser.add_argument("action", nargs="?", choices=["break", "restore"])
    args = parser.parse_args()
    if args.switch == "status":
        status()
    elif args.action is None:
        parser.error("action is required: break or restore")
    elif args.switch == "prompt":
        switch_prompt(args.action)
    else:
        switch_route(args.action)
