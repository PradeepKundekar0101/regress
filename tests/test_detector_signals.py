from regress_mcp.detector import SIGNAL_BY_NAME, SIGNALS


def test_refusal_rate_is_evidence_only():
    assert SIGNAL_BY_NAME["refusal_rate"].alarms is False
    alarming = {s.name for s in SIGNALS if s.alarms}
    assert "refusal_rate" not in alarming
    assert {"eval_score", "format_valid", "escalation_correct", "latency_p95_ms", "cost_per_request_usd"} <= alarming
