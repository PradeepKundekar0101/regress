"""The sandbox harness template must score exactly like the server, or honest reports get rejected."""
import importlib.util
import json
import sys
import types
from pathlib import Path

from target import config
from target.bot.scoring import score as server_score

HARNESS = Path(__file__).resolve().parent.parent / "skills" / "regress-runbook" / "replay_harness.py"
sys.modules.setdefault("mcp_client", types.SimpleNamespace(call_tool=None))
spec = importlib.util.spec_from_file_location("replay_harness", HARNESS)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)

CASES = [
    json.dumps({"answer": "Fee is Rs 499 plus GST.", "citations": ["kb-05"], "escalate": False, "confidence": 0.9}),
    json.dumps({"answer": "Fee is Rs 499.", "kb_ids": ["kb-05"]}),
    "```json\n" + json.dumps({"answer": "Rs 499", "citations": ["kb-05"], "escalate": False, "confidence": 1}) + "\n```",
    "The annual fee is Rs 499.",
    json.dumps({"answer": "I can’t give personal investment advice.", "citations": [], "escalate": False, "confidence": 0.8}),
    json.dumps({"answer": "ok", "citations": ["kb-10"], "escalate": "yes", "confidence": 0.5}),
    json.dumps({"answer": "", "citations": [], "escalate": True, "confidence": 2}),
    "[1, 2]",
    "",
]


def expected_of(item):
    return {"escalate": item.expect_escalate, "refusal": item.expect_refusal,
            "citations": item.expect_citations, "must_contain": item.must_contain}


def test_harness_matches_server_scoring_on_every_golden_item():
    for item in config.golden_items().values():
        for raw in CASES:
            ours, theirs = harness.score(raw, expected_of(item)), server_score(raw, item)
            assert ours["eval"] == theirs.eval_score, (item.id, raw)
            assert ours["format_valid"] == theirs.format_valid, (item.id, raw)
