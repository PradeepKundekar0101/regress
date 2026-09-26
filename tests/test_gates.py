from regress_mcp.gates import evaluate

QUALITY = ["eval_score", "format_valid", "escalation_correct", "citation_correct"]
LATENCY = ["latency_p50_ms", "latency_p95_ms", "cost_per_request_usd"]
REPLAY_PROMPT = {"verified": True, "eval_gap": 0.42, "latency_ratio": 1.05, "coverage": "30/30",
                 "evidence_ids": {"gap": "ev_gap"}}
REPLAY_ROUTE = {"verified": True, "eval_gap": 0.0, "latency_ratio": 5.9, "coverage": "20/20",
                "evidence_ids": {"gap": "ev_gap"}}


def change(cid, ts, kind, frm, to, by_regress=False):
    target = "adopt-support:production" if kind == "prompt" else "support"
    return {"change_id": cid, "ts": f"2026-09-25T16:{ts}+00:00", "kind": kind, "target": target,
            "from": frm, "to": to, "by_regress": by_regress, "commit": None, "evidence": f"ev_c{cid}"}


def cand(dim, value, explains, still=()):
    return {"dimension": dim, "value": value, "explains": list(explains),
            "still_alarming_without_it": list(still), "inconclusive": [],
            "z_after_exclusion": {a: {"z": 0.1, "evidence": f"ev_z_{a}"} for a in list(explains) + list(still)}}


def test_clean_prompt_regression_passes_all_gates():
    loc = {"candidates": [cand("prompt_version", "2", QUALITY)]}
    result = evaluate(candidate={"dimension": "prompt_version", "value": "2"}, localization=loc,
                      onset="2026-09-25T16:02:30+00:00", changes=[change(1, "01:36", "prompt", "v1", "v2")],
                      replay_check=REPLAY_PROMPT, live={"prompt_version": 2, "model": "gpt-4.1-mini"})
    assert [g["passed"] for g in result["gates"]] == [True, True, True, True]
    assert result["verdict"] == "LOCALIZED_PROMPT"


def test_onset_long_after_change_fails_gate_1():
    loc = {"candidates": [cand("prompt_version", "2", QUALITY)]}
    result = evaluate(candidate={"dimension": "prompt_version", "value": "2"}, localization=loc,
                      onset="2026-09-25T16:25:00+00:00", changes=[change(1, "01:36", "prompt", "v1", "v2")],
                      replay_check=REPLAY_PROMPT, live={"prompt_version": 2, "model": "gpt-4.1-mini"})
    assert not result["gates"][0]["passed"]
    assert result["verdict"] == "NOT_LOCALIZED"


def test_replay_without_gap_fails_gate_2():
    loc = {"candidates": [cand("prompt_version", "2", QUALITY)]}
    flat = {**REPLAY_PROMPT, "eval_gap": 0.03}
    result = evaluate(candidate={"dimension": "prompt_version", "value": "2"}, localization=loc,
                      onset="2026-09-25T16:02:30+00:00", changes=[change(1, "01:36", "prompt", "v1", "v2")],
                      replay_check=flat, live={"prompt_version": 2, "model": "gpt-4.1-mini"})
    assert not result["gates"][1]["passed"] and result["verdict"] == "NOT_LOCALIZED"


def test_live_competing_change_fails_gate_4():
    loc = {"candidates": [cand("prompt_version", "2", QUALITY)]}
    changes = [change(1, "01:36", "prompt", "v1", "v2"), change(2, "01:50", "route", "gpt-4.1-mini", "gpt-5")]
    result = evaluate(candidate={"dimension": "prompt_version", "value": "2"}, localization=loc,
                      onset="2026-09-25T16:02:30+00:00", changes=changes,
                      replay_check=REPLAY_PROMPT, live={"prompt_version": 2, "model": "gpt-5"})
    assert not result["gates"][3]["passed"]
    assert "route gpt-4.1-mini->gpt-5" in result["gates"][3]["detail"]


def test_route_regression_overlapping_a_rolled_back_prompt_regression():
    """The demo sequence: prompt fault, Regress rolls it back, route fault within the same window."""
    loc = {"candidates": [cand("prompt_version", "2", QUALITY, still=LATENCY),
                          cand("model", "gpt-5", LATENCY, still=QUALITY)]}
    changes = [change(1, "01:36", "prompt", "v1", "v2"),
               change(2, "02:26", "prompt", "v2", "v1", by_regress=True),
               change(3, "02:38", "route", "gpt-4.1-mini", "gpt-5")]
    result = evaluate(candidate={"dimension": "model", "value": "gpt-5"}, localization=loc,
                      onset="2026-09-25T16:04:00+00:00", changes=changes,
                      replay_check=REPLAY_ROUTE, live={"prompt_version": 1, "model": "gpt-5"})
    assert [g["passed"] for g in result["gates"]] == [True, True, True, True]
    assert "prompt_version=2 (no longer live)" in result["gates"][2]["detail"]
    assert result["verdict"] == "LOCALIZED_ROUTE"


def test_made_and_undone_manual_change_is_not_a_competitor():
    loc = {"candidates": [cand("model", "gpt-5", LATENCY)]}
    changes = [change(1, "01:36", "prompt", "v1", "v2"), change(2, "02:26", "prompt", "v2", "v1"),
               change(3, "02:38", "route", "gpt-4.1-mini", "gpt-5")]
    result = evaluate(candidate={"dimension": "model", "value": "gpt-5"}, localization=loc,
                      onset="2026-09-25T16:04:00+00:00", changes=changes,
                      replay_check=REPLAY_ROUTE, live={"prompt_version": 1, "model": "gpt-5"})
    assert result["gates"][3]["passed"]


def test_segment_that_explains_nothing_fails_gate_3():
    loc = {"candidates": [cand("prompt_version", "2", QUALITY)]}
    result = evaluate(candidate={"dimension": "model", "value": "gpt-5"}, localization=loc,
                      onset="2026-09-25T16:04:00+00:00", changes=[change(3, "02:38", "route", "gpt-4.1-mini", "gpt-5")],
                      replay_check=REPLAY_ROUTE, live={"prompt_version": 2, "model": "gpt-5"})
    assert not result["gates"][2]["passed"]


def test_recovery_effect_check_per_signal_kind():
    from regress_mcp.actions import _within_effect
    assert _within_effect("format_valid", 0.95, 1.0)            # 5 points off: back in band
    assert not _within_effect("format_valid", 0.60, 1.0)        # 40 points off: still degraded
    assert _within_effect("latency_p95_ms", 2900.0, 2600.0)     # 1.1x
    assert not _within_effect("latency_p95_ms", 7000.0, 2600.0)  # 2.7x
