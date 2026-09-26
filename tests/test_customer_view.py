"""Customer view: the facts read back from a reply slip, and the tool's phase guards and failure handling."""
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from regress_mcp import customer_view, server
from regress_mcp.store import Store

# The markup renderSlip() in target/bot/static/index.html produces.
SLIP_V1 = """<article class="slip"><div class="who"><svg class="spark"></svg>Ada, Adopt Help</div>
<div class="handoff">Passed to a specialist. They will contact you shortly.</div>
<div class="answer">Please freeze your card immediately from Cards &gt; Controls.</div>
<div class="ledger"><div class="ledger-title">Sources</div><ul aria-label="Sources">
<li><span class="id">kb-10</span><span>Unauthorised transactions and fraud</span></li></ul></div>
<footer><span class="meta">Prompt v1 · gpt-4.1-mini · 1275 ms</span>
<button class="icon-btn" type="button" aria-label="Helpful"></button></footer></article>"""
SLIP_V2 = """<article class="slip"><div class="who">Ada, Adopt Help</div>
<div class="answer">Oh no, I'm so sorry to hear that! Let's get this sorted together.</div>
<footer><span class="meta">Prompt v2 · gpt-4.1-mini · 2,104 ms</span>
<button class="icon-btn" type="button">Talk to a person</button></footer></article>"""


def test_observe_reads_banner_citations_and_footer():
    seen = customer_view.observe(SLIP_V1)
    assert seen["banner"] is True
    assert seen["citations"] == ["kb-10"]
    assert (seen["prompt_version"], seen["model"], seen["latency_ms"]) == (1, "gpt-4.1-mini", 1275)


def test_observe_regressed_reply_has_no_banner_and_no_sources():
    seen = customer_view.observe(SLIP_V2)
    assert seen["banner"] is False
    assert seen["citations"] == []
    assert (seen["prompt_version"], seen["latency_ms"]) == (2, 2104)


def test_answer_text_mentioning_a_specialist_is_not_the_banner():
    seen = customer_view.observe(SLIP_V2.replace("sorted together", "sorted with a specialist"))
    assert seen["banner"] is False


@pytest.mark.parametrize("bad", ["../etc", "inc_1/../../x", "inc_20260926_085527_4fce/..", ""])
def test_media_dir_rejects_anything_but_an_incident_id(bad):
    with pytest.raises(ValueError):
        customer_view.media_dir(bad)


def test_media_files_rejects_unknown_phase():
    with pytest.raises(ValueError):
        customer_view.media_files("inc_20260926_085527_4fce", "during")


@pytest.fixture
def tool_store(tmp_path, monkeypatch, checkpointed):
    store, inc = checkpointed
    monkeypatch.setattr(server, "store", store)
    return store, inc


def _fake_capture(tmp_path: Path, slip: str):
    def capture(incident_id, phase):
        video, shot = tmp_path / f"{phase}.mp4", tmp_path / f"{phase}.png"
        video.write_bytes(b"v"), shot.write_bytes(b"p")
        return {**customer_view.observe(slip), "video": video, "screenshot": shot,
                "question": customer_view.QUESTION, "url": "http://bot/?probe=1"}
    return capture


def test_before_is_captured_at_checkpointed_with_evidence(tool_store, tmp_path, monkeypatch):
    store, inc = tool_store
    monkeypatch.setattr(customer_view, "capture", _fake_capture(tmp_path, SLIP_V2))
    out = server.capture_customer_view(inc, "before")
    assert out["captured"] is True and out["banner"] is False and out["prompt_version"] == 2
    labels = {e["label"]: e for e in store.evidence(inc).values()}
    banner = labels["customer view before: specialist banner shown"]
    assert banner["value"] == 0 and banner["source"]["kind"] == "video"
    assert banner["computed_by"] == "regress-mcp/customer_view"
    assert labels["customer view before: sources cited (none)"]["value"] == 0
    assert store.incident(inc)["status"] == "checkpointed"


def test_wrong_phase_for_status_is_refused(tool_store):
    _, inc = tool_store
    with pytest.raises(ToolError, match="captured when it is verified"):
        server.capture_customer_view(inc, "after")
    with pytest.raises(ToolError, match="phase must be one of"):
        server.capture_customer_view(inc, "during")


def test_capture_failure_is_reported_not_raised(tool_store, monkeypatch):
    store, inc = tool_store

    def boom(incident_id, phase):
        raise TimeoutError("no reply slip in 45 s")
    monkeypatch.setattr(customer_view, "capture", boom)
    out = server.capture_customer_view(inc, "before")
    assert out == {"captured": False, "reason": "TimeoutError: no reply slip in 45 s"}
    assert store.incident(inc)["status"] == "checkpointed"
    assert store.evidence(inc) == {}
