"""Replay: re-run real inputs under two arms (prompt version x model) and cross-check the agent's analysis.

The model calls happen here, where the credentials are, through the same function production uses.
The agent's sandbox code scores the stored outputs and reports per-arm numbers; `verify_report`
re-scores the same outputs deterministically and rejects the report if the numbers disagree.
"""

import statistics
from concurrent.futures import ThreadPoolExecutor

from langfuse import get_client
from openai import OpenAIError

from regress_mcp.store import Evidence, Store
from target import config
from target.bot import llm
from target.bot.scoring import score

CONCURRENCY = 16
TOLERANCE_ABS = 0.005     # eval, rates
TOLERANCE_REL = 0.01      # latency


def generate(store: Store, incident_id: str, arms: list[dict], inputs: list[dict]) -> dict:
    """arms: [{"name", "prompt_version", "model"}]; inputs: [{"trace_id", "golden_id", "question"}]."""
    langfuse = get_client()
    client = llm.make_client(max_retries=2)
    golden = config.golden_items()
    systems = {
        arm["name"]: langfuse.get_prompt(config.prompt_name(), version=arm["prompt_version"],
                                         cache_ttl_seconds=0).compile(kb=config.kb_text())
        for arm in arms
    }

    def run(job):
        arm, item = job
        out = {"trace_id": item["trace_id"], "golden_id": item["golden_id"], "arm": arm["name"],
               "prompt_version": arm["prompt_version"], "model": arm["model"]}
        try:
            g = llm.generate(client, arm["model"], systems[arm["name"]], item["question"])
            return {**out, "raw": g.raw, "latency_ms": g.latency_ms, "tokens_in": g.tokens_in,
                    "tokens_out": g.tokens_out, "cost_usd": g.cost_usd, "error": None}
        except OpenAIError as exc:
            return {**out, "raw": None, "latency_ms": None, "error": str(exc)[:300]}

    jobs = [(arm, item) for item in inputs for arm in arms]
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        outputs = list(pool.map(run, jobs))
    for o in outputs:
        item = golden[o["golden_id"]]
        o["expected"] = {"escalate": item.expect_escalate, "refusal": item.expect_refusal,
                         "citations": item.expect_citations, "must_contain": item.must_contain}
    spec = {"arms": arms, "trace_ids": [i["trace_id"] for i in inputs]}
    replay_id = store.save_replay(incident_id, spec, outputs)
    failed = sum(1 for o in outputs if o["error"])
    return {"replay_id": replay_id, "arms": arms, "inputs": len(inputs), "calls": len(outputs),
            "failed_calls": failed, "outputs": outputs}


def arm_stats(outputs: list[dict]) -> dict[str, dict]:
    """The reference computation `verify_report` holds the agent's numbers to."""
    golden = config.golden_items()
    stats = {}
    for arm in sorted({o["arm"] for o in outputs}):
        ok = [o for o in outputs if o["arm"] == arm and not o["error"]]
        scored = [score(o["raw"], golden[o["golden_id"]]) for o in ok]
        stats[arm] = {
            "n": len(ok),
            "failed": sum(1 for o in outputs if o["arm"] == arm and o["error"]),
            "eval_score_mean": statistics.fmean(s.eval_score for s in scored) if scored else None,
            "format_valid_rate": statistics.fmean(s.format_valid for s in scored) if scored else None,
            "escalation_correct_rate": statistics.fmean(s.escalation_correct for s in scored) if scored else None,
            "latency_p50_ms": statistics.median(o["latency_ms"] for o in ok) if ok else None,
            "cost_per_request_usd": statistics.fmean(o["cost_usd"] for o in ok if o["cost_usd"] is not None)
            if any(o["cost_usd"] is not None for o in ok) else None,
        }
    return stats


def verify_report(store: Store, replay_id: str, report: dict, baseline_arm: str, suspect_arm: str) -> dict:
    """Accept the sandbox report only if it matches the deterministic re-computation."""
    replay = store.replay(replay_id)
    reference = arm_stats(replay["outputs"])
    mismatches = []
    for arm, ref in reference.items():
        got = (report.get("arms") or {}).get(arm)
        if got is None:
            mismatches.append(f"report has no arm '{arm}'")
            continue
        for key in ("n", "eval_score_mean", "format_valid_rate", "latency_p50_ms"):
            want, have = ref[key], got.get(key)
            if want is None:
                continue
            if not isinstance(have, (int, float)):
                mismatches.append(f"{arm}.{key} missing")
                continue
            tol = TOLERANCE_REL * abs(want) if key == "latency_p50_ms" else TOLERANCE_ABS
            if abs(have - want) > tol:
                mismatches.append(f"{arm}.{key}: report {have} vs recomputed {round(want, 4)}")

    base, sus = reference.get(baseline_arm), reference.get(suspect_arm)
    if base is None or sus is None:
        mismatches.append(f"arms must include {baseline_arm} and {suspect_arm}")
    source = {"kind": "replay", "replay_id": replay_id, "trace_ids": replay["spec"]["trace_ids"]}
    evidence, gap = [], {}
    if base and sus and not mismatches:
        for arm_name, st in ((baseline_arm, base), (suspect_arm, sus)):
            for key, unit in (("eval_score_mean", "score"), ("format_valid_rate", "ratio"),
                              ("latency_p50_ms", "ms"), ("cost_per_request_usd", "usd")):
                if st[key] is not None:
                    ev = Evidence(f"replay {arm_name} {key} (n={st['n']})", st[key], unit, source,
                                  "sandbox replay harness, re-verified by regress-mcp/replay")
                    evidence.append(ev)
                    gap[f"{arm_name} {key}"] = ev.id
        eval_gap = base["eval_score_mean"] - sus["eval_score_mean"]
        latency_ratio = sus["latency_p50_ms"] / base["latency_p50_ms"] if base["latency_p50_ms"] else None
        cost_ratio = (sus["cost_per_request_usd"] / base["cost_per_request_usd"]
                      if base["cost_per_request_usd"] and sus["cost_per_request_usd"] else None)
        for label, value, unit in (("replay eval score gap (baseline - suspect)", eval_gap, "score"),
                                   ("replay latency p50 ratio (suspect / baseline)", latency_ratio, "x"),
                                   ("replay cost ratio (suspect / baseline)", cost_ratio, "x")):
            if value is not None:
                ev = Evidence(label, round(value, 4), unit, source, "regress-mcp/replay")
                evidence.append(ev)
                gap[label] = ev.id
        coverage = f"{sus['n']}/{sus['n'] + sus['failed']}"
        return {"verified": True, "reference": reference, "eval_gap": eval_gap, "latency_ratio": latency_ratio,
                "cost_ratio": cost_ratio, "coverage": coverage, "evidence_ids": gap, "evidence": evidence}
    return {"verified": False, "mismatches": mismatches, "reference": reference, "evidence": []}
