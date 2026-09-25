"""Look up prompt versions in Langfuse by the `variant` tag stored in each version's config."""

from langfuse import Langfuse
from langfuse.api.core.api_error import ApiError


def list_versions(langfuse: Langfuse, name: str) -> list[int]:
    metas = langfuse.api.prompts.list(name=name, limit=50).data
    return sorted(v for meta in metas if meta.name == name for v in meta.versions)


def find_version(langfuse: Langfuse, name: str, variant: str) -> int | None:
    """Newest version whose config says {"variant": variant}, or None."""
    try:
        versions = list_versions(langfuse, name)
    except ApiError as exc:
        if exc.status_code == 404:
            return None
        raise
    for version in reversed(versions):
        prompt = langfuse.api.prompts.get(name, version=version)
        if (prompt.config or {}).get("variant") == variant:
            return version
    return None


def production_version(langfuse: Langfuse, name: str) -> int:
    return langfuse.api.prompts.get(name, label="production").version
