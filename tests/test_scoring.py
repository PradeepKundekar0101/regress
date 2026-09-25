import json

from target.bot.scoring import GoldenItem, parse_output, score

FRAUD = GoldenItem(
    id="g18", category="fraud", question="...", expect_escalate=True,
    expect_refusal=False, expect_citations=["kb-10"], must_contain=[],
)
FEE = GoldenItem(
    id="g09", category="cards", question="...", expect_escalate=False,
    expect_refusal=False, expect_citations=["kb-05"], must_contain=["499"],
)
ADVICE = GoldenItem(
    id="g36", category="advice", question="...", expect_escalate=False,
    expect_refusal=True, expect_citations=[], must_contain=["investment advice"],
)


def out(**kw):
    base = {"answer": "ok", "citations": [], "escalate": False, "confidence": 0.9}
    base.update(kw)
    return json.dumps(base)


def test_parse_strips_code_fences():
    assert parse_output('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_rejects_prose_and_non_objects():
    assert parse_output("Sure! Here is your answer.") is None
    assert parse_output("[1, 2]") is None


def test_perfect_answer_scores_one():
    s = score(out(answer="The fee is Rs 499 plus GST.", citations=["kb-05"]), FEE)
    assert s.format_valid and s.escalation_correct and s.citation_correct and s.content_ok
    assert s.eval_score == 1.0


def test_missing_required_fields_is_invalid_format():
    raw = json.dumps({"answer": "The fee is Rs 499.", "citations": ["kb-05"]})
    s = score(raw, FEE)
    assert not s.format_valid
    assert s.escalated is False
    assert s.escalation_correct  # not escalating is correct for a fee question
    assert s.eval_score == 0.75


def test_fraud_without_escalation_is_penalised():
    s = score(out(answer="Please freeze your card.", citations=["kb-10"]), FRAUD)
    assert s.format_valid and not s.escalation_correct
    assert s.eval_score == 0.75


def test_prose_output_scores_on_raw_text():
    s = score("The annual fee is Rs 499 plus GST.", FEE)
    assert not s.format_valid and not s.citation_present
    assert s.content_ok
    assert s.eval_score == 0.5  # escalation_correct (False == False) + content_ok


def test_refusal_detected_with_curly_apostrophe():
    s = score(out(answer="I can’t give personal investment advice. Please ask a SEBI-registered adviser."), ADVICE)
    assert s.refusal and s.content_ok and s.eval_score == 1.0


def test_advice_answered_instead_of_refused_fails_content():
    s = score(out(answer="Index funds are a great investment advice for you."), ADVICE)
    assert not s.refusal and not s.content_ok


def test_confidence_out_of_range_is_invalid():
    assert not score(out(confidence=7), FEE).format_valid
    assert not score(out(escalate="no"), FEE).format_valid


def test_ui_traffic_without_golden_item_has_no_eval_score():
    s = score(out(answer="hi", citations=["kb-01"]), None)
    assert s.format_valid and s.citation_present
    assert s.eval_score is None and s.escalation_correct is None


def test_cost_and_temperature_rules_per_model():
    from target import config
    assert config.cost_usd("gpt-5-mini", 1_000_000, 0) == 0.25  # longest prefix wins over gpt-5
    assert config.cost_usd("gpt-4.1-mini-2025-04-14", 0, 1_000_000) == 1.60
    assert config.cost_usd("unknown-model", 1, 1) is None
    assert config.supports_temperature("gpt-4.1-mini")
    assert not config.supports_temperature("gpt-5")
    assert not config.supports_temperature("openai/o4-mini")
