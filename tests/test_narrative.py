from regress_mcp.narrative import bare_numbers, validate

EVIDENCE = {
    "ev_eval": {"value": 0.58, "unit": "score", "label": "replay eval v2"},
    "ev_fmt": {"value": 0.4, "unit": "ratio", "label": "format valid"},
    "ev_p95": {"value": 11026.4, "unit": "ms", "label": "latency p95"},
}


def test_placeholders_are_substituted():
    result = validate("Prompt v2 scored {{ev_eval}} on replay; format validity fell to {{ev_fmt}}.", EVIDENCE)
    assert result["accepted"]
    assert result["rendered"] == "Prompt v2 scored 0.58 on replay; format validity fell to 40.0%."
    assert result["cited"] == ["ev_eval", "ev_fmt"]


def test_hallucinated_number_is_rejected():
    result = validate("Prompt v2 scored {{ev_eval}}, down from 0.91.", EVIDENCE)
    assert not result["accepted"]
    assert "0.91" in result["reasons"][0]


def test_unknown_placeholder_is_rejected():
    result = validate("Latency rose to {{ev_made_up}}.", EVIDENCE)
    assert not result["accepted"]
    assert "ev_made_up" in result["reasons"][0]


def test_identifiers_with_digits_are_allowed():
    text = "Rolled adopt-support back from v2 to v1 on gpt-4.1-mini; kb-10 and p95 unchanged; gpt-5 route ruled out."
    assert bare_numbers(text) == []
    assert validate(text, EVIDENCE)["accepted"]


def test_disguised_numbers_are_rejected():
    assert bare_numbers("latency 2x higher, 40% invalid, Rs 499 fee, at 14:05, (50 traces), ₹12,000") == [
        "2x", "40%", "499", "14:05", "50", "₹12,000"]


def test_malformed_placeholder_is_rejected():
    result = validate("Latency rose to {{ ev-p95 }}.", EVIDENCE)
    assert not result["accepted"]
    assert "malformed" in result["reasons"][0]
