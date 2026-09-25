"""Evidence-only narration. The narrator may state a number only as a {{ev_...}} placeholder.

`validate` substitutes each placeholder with the formatted evidence value, and rejects the text if a
placeholder does not resolve or if any bare numeric token remains. Identifiers that contain digits
(v2, gpt-5, p95, kb-10) are allowed because they start with a letter; numbers such as 0.58, 40%,
2x, Rs 499 or 14:05 are not. `template` is the deterministic fallback after one failed retry.
"""

import re
from datetime import datetime, timezone

PLACEHOLDER = re.compile(r"\{\{\s*(ev_\w+)\s*\}\}")
BRACES = re.compile(r"\{\{|\}\}")
EDGE_PUNCT = "()[]{}<>,.;:!?\"'*_`"


def fmt(ev: dict) -> str:
    value, unit = ev["value"], ev["unit"]
    if value is None:
        return "n/a"
    if unit == "ratio":
        return f"{value * 100:.1f}%"
    if unit == "score":
        return f"{value:.2f}"
    if unit == "ms":
        return f"{value:,.0f} ms"
    if unit == "usd":
        return f"${value:.5f}"
    if unit in ("x",):
        return f"{value:.2f}x"
    if unit == "z":
        return f"{value:+.1f}"
    if unit == "unix_ts":
        return datetime.fromtimestamp(value, timezone.utc).strftime("%H:%M:%S UTC")
    if unit == "count":
        return f"{value:,.0f}"
    return f"{value:g}"


def bare_numbers(text: str) -> list[str]:
    """Tokens that are numbers rather than identifiers: they start with a non-letter and contain a digit."""
    found = []
    for token in text.split():
        token = token.strip(EDGE_PUNCT)
        if not token or not any(ch.isdigit() for ch in token):
            continue
        if not token[0].isalpha():
            found.append(token)
    return found


def validate(text: str, evidence: dict[str, dict]) -> dict:
    unknown = [eid for eid in PLACEHOLDER.findall(text) if eid not in evidence]
    remainder = PLACEHOLDER.sub(" ", text)
    stray = bare_numbers(remainder)
    malformed = bool(BRACES.search(remainder))
    if unknown or stray or malformed:
        reasons = []
        if malformed:
            reasons.append("malformed placeholder: use {{ev_<id>}} exactly")
        if unknown:
            reasons.append(f"placeholders with no evidence: {unknown}")
        if stray:
            reasons.append(f"numbers not taken from evidence: {stray}")
        return {"accepted": False, "reasons": reasons}
    rendered = PLACEHOLDER.sub(lambda m: fmt(evidence[m.group(1)]), text)
    return {"accepted": True, "rendered": rendered, "cited": sorted(set(PLACEHOLDER.findall(text)))}


def template(incident: dict, gate_result: dict | None, evidence: dict[str, dict]) -> str:
    """Deterministic report used when the narrator fails validation twice."""
    lines = [f"Incident {incident['id']}: status {incident['status']}, verdict {incident.get('verdict') or 'pending'}."]
    if gate_result:
        change = gate_result.get("change")
        if change:
            lines.append(f"Candidate change: {change['kind']} {change['target']} {change['from']} -> {change['to']} "
                         f"({(change.get('commit') or {}).get('message', 'no commit message')}).")
        for gate in gate_result["gates"]:
            lines.append(f"Gate {gate['gate']} ({gate['name']}): {'passed' if gate['passed'] else 'FAILED'} - {gate['detail']}.")
    for ev in list(evidence.values())[:12]:
        lines.append(f"- {ev['label']}: {fmt(ev)}")
    return "\n".join(lines)
