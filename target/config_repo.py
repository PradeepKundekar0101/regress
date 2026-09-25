"""Mirror runtime config into the CONFIG_REPO GitHub repo, one commit per change.

The repo holds `prompts/<name>.json` (which version is labelled production) and
`routes/<route>.json` (which model serves the route). Its history is the "what changed" record
the investigating agent reads, alongside Langfuse prompt versions.
"""

import base64
import json
import subprocess

import httpx

from target import config

API = "https://api.github.com"


def _token() -> str:
    token = config.optional_env("GITHUB_TOKEN")
    if token:
        return token
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


def enabled() -> bool:
    return config.optional_env("CONFIG_REPO") is not None


def commit_json(path: str, content: dict, message: str) -> str | None:
    """Create or update `path` with `content`. Returns the commit SHA, or None if unchanged or disabled."""
    repo = config.optional_env("CONFIG_REPO")
    if repo is None:
        return None
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json"}
    body = json.dumps(content, indent=2) + "\n"
    with httpx.Client(base_url=API, headers=headers, timeout=20) as gh:
        current = gh.get(f"/repos/{repo}/contents/{path}")
        sha = None
        if current.status_code == 200:
            existing = current.json()
            if base64.b64decode(existing["content"]).decode() == body:
                return None
            sha = existing["sha"]
        elif current.status_code != 404:
            current.raise_for_status()
        resp = gh.put(f"/repos/{repo}/contents/{path}", json={
            "message": message,
            "content": base64.b64encode(body.encode()).decode(),
            **({"sha": sha} if sha else {}),
        })
        resp.raise_for_status()
        return resp.json()["commit"]["sha"]


def record_prompt(name: str, version: int, message: str) -> str | None:
    return commit_json(f"prompts/{name}.json", {"prompt": name, "production_version": version}, message)


def record_route(route: str, model: str, message: str) -> str | None:
    return commit_json(f"routes/{route}.json", {"route": route, "model": model}, message)
