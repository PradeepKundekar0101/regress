"""The chatbot's repository (CONFIG_REPO): the production prompt text and model route, one commit per change.

    prompts/adopt-support.md     the full production prompt template, so every change is a readable diff
    prompts/adopt-support.json   which Langfuse version that text is (Langfuse serves it at runtime)
    routes/support.json          which model serves the support route

A fault is a commit whose diff removes instructions; Regress's fix is a commit whose diff puts them back.
Files change together in a single commit (Git Data API), so one diff tells the whole story.
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


def _client() -> httpx.Client:
    return httpx.Client(base_url=API, timeout=30, headers={
        "Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json"})


def read_file(path: str, ref: str | None = None) -> str | None:
    repo = config.optional_env("CONFIG_REPO")
    if repo is None:
        return None
    with _client() as gh:
        resp = gh.get(f"/repos/{repo}/contents/{path}", params={"ref": ref} if ref else {})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return base64.b64decode(resp.json()["content"]).decode()


def commit_files(files: dict[str, str], message: str) -> str | None:
    """Write several files in one commit on the default branch. Returns the SHA, or None if nothing changed."""
    repo = config.optional_env("CONFIG_REPO")
    if repo is None:
        return None
    with _client() as gh:
        branch = gh.get(f"/repos/{repo}").raise_for_status().json()["default_branch"]
        head = gh.get(f"/repos/{repo}/git/ref/heads/{branch}").raise_for_status().json()["object"]["sha"]
        base_tree = gh.get(f"/repos/{repo}/git/commits/{head}").raise_for_status().json()["tree"]["sha"]
        changed = []
        for path, body in files.items():
            current = gh.get(f"/repos/{repo}/contents/{path}", params={"ref": head})
            if current.status_code == 200 and base64.b64decode(current.json()["content"]).decode() == body:
                continue
            changed.append({"path": path, "mode": "100644", "type": "blob", "content": body})
        if not changed:
            return None
        tree = gh.post(f"/repos/{repo}/git/trees", json={"base_tree": base_tree, "tree": changed}).raise_for_status().json()
        commit = gh.post(f"/repos/{repo}/git/commits",
                         json={"message": message, "tree": tree["sha"], "parents": [head]}).raise_for_status().json()
        gh.patch(f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": commit["sha"]}).raise_for_status()
        return commit["sha"]


def commit_json(path: str, content: dict, message: str) -> str | None:
    return commit_files({path: json.dumps(content, indent=2) + "\n"}, message)


def record_prompt(name: str, version: int, message: str, text: str | None = None) -> str | None:
    """Commit the full text of prompt `version` as the production prompt, with its version pointer."""
    if text is None:
        from langfuse import get_client
        text = get_client().api.prompts.get(name, version=version).prompt
    return commit_files({
        f"prompts/{name}.md": text if text.endswith("\n") else text + "\n",
        f"prompts/{name}.json": json.dumps({"prompt": name, "production_version": version}, indent=2) + "\n",
    }, message)


def record_route(route: str, model: str, message: str) -> str | None:
    return commit_json(f"routes/{route}.json", {"route": route, "model": model}, message)


def commit_diff(sha: str) -> dict | None:
    """Files and unified-diff patches of one commit, for the console and the report."""
    repo = config.optional_env("CONFIG_REPO")
    if repo is None:
        return None
    with _client() as gh:
        c = gh.get(f"/repos/{repo}/commits/{sha}").raise_for_status().json()
    return {"sha": c["sha"], "message": c["commit"]["message"], "url": c["html_url"],
            "author": c["commit"]["author"]["name"], "ts": c["commit"]["author"]["date"],
            "files": [{"path": f["filename"], "additions": f["additions"], "deletions": f["deletions"],
                       "patch": f.get("patch", "")} for f in c.get("files", [])]}
