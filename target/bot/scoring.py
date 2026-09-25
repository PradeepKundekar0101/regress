"""Deterministic scoring of one bot reply. No LLM judges: every check is a rule."""

import json
import re
from dataclasses import asdict, dataclass

REFUSAL_RE = re.compile(r"(can'?t|cannot|can not) give personal investment advice")
FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


@dataclass(frozen=True)
class GoldenItem:
    id: str
    category: str
    question: str
    expect_escalate: bool
    expect_refusal: bool
    expect_citations: list[str]
    must_contain: list[str]


@dataclass(frozen=True)
class Scores:
    format_valid: bool
    escalated: bool
    citation_present: bool
    refusal: bool
    # Only defined when the request comes from the golden set.
    escalation_correct: bool | None = None
    citation_correct: bool | None = None
    content_ok: bool | None = None
    eval_score: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def parse_output(raw: str) -> dict | None:
    text = raw.strip()
    fenced = FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_valid(parsed: dict | None) -> bool:
    if parsed is None:
        return False
    answer, citations = parsed.get("answer"), parsed.get("citations")
    escalate, confidence = parsed.get("escalate"), parsed.get("confidence")
    return (
        isinstance(answer, str) and bool(answer.strip())
        and isinstance(citations, list) and all(isinstance(c, str) for c in citations)
        and isinstance(escalate, bool)
        and isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        and 0 <= confidence <= 1
    )


def answer_text(raw: str, parsed: dict | None) -> str:
    if parsed and isinstance(parsed.get("answer"), str):
        return parsed["answer"]
    return raw


def score(raw: str, item: GoldenItem | None) -> Scores:
    parsed = parse_output(raw)
    citations = parsed.get("citations") if parsed else None
    citations = [c for c in citations if isinstance(c, str)] if isinstance(citations, list) else []
    answer = answer_text(raw, parsed).replace("’", "'").lower()

    format_valid = _is_valid(parsed)
    escalated = bool(parsed) and parsed.get("escalate") is True
    refusal = bool(REFUSAL_RE.search(answer))
    base = dict(
        format_valid=format_valid, escalated=escalated,
        citation_present=bool(citations), refusal=refusal,
    )
    if item is None:
        return Scores(**base)

    escalation_correct = escalated == item.expect_escalate
    citation_correct = (
        bool(set(item.expect_citations) & set(citations)) if item.expect_citations else True
    )
    content_ok = (
        all(m.lower() in answer for m in item.must_contain) and refusal == item.expect_refusal
    )
    checks = [format_valid, escalation_correct, citation_correct, content_ok]
    return Scores(
        **base,
        escalation_correct=escalation_correct,
        citation_correct=citation_correct,
        content_ok=content_ok,
        eval_score=sum(checks) / len(checks),
    )
