import pytest

from regress_mcp.store import Evidence, Store, TransitionError


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state.sqlite")


def ev(label="x", value=1.0):
    return Evidence(label=label, value=value, unit="ratio", source={"kind": "sql", "query": "select 1"},
                    computed_by="test")


def test_happy_path_records_every_transition(store):
    [e] = store.add_evidence(None, [ev()])
    inc = store.open_incident("eval_score", [e["id"]])
    for status in ["planned", "replayed", "checkpointed", "approved", "applied", "verified"]:
        store.transition(inc, status, f"to {status}", [])
    result = store.incident(inc)
    assert result["status"] == "verified"
    assert [t["to_status"] for t in result["transitions"]] == [
        "detected", "planned", "replayed", "checkpointed", "approved", "applied", "verified"]
    assert store.evidence(inc)[e["id"]]["value"] == 1.0


def test_cannot_skip_to_applied(store):
    inc = store.open_incident("eval_score", [])
    with pytest.raises(TransitionError):
        store.transition(inc, "applied", "shortcut", [])
    assert store.incident(inc)["status"] == "detected"


def test_failed_gates_cannot_reach_approval(store):
    inc = store.open_incident("eval_score", [])
    store.transition(inc, "planned", "", [])
    store.transition(inc, "replayed", "", [])
    store.transition(inc, "not_localized", "gate 2 failed", [])
    with pytest.raises(TransitionError):
        store.transition(inc, "checkpointed", "retry", [])
    with pytest.raises(TransitionError):
        store.transition(inc, "approved", "retry", [])


def test_proposal_is_frozen_json_and_terminal_incidents_are_not_open(store):
    inc = store.open_incident("eval_score", [])
    store.transition(inc, "planned", "", [])
    store.transition(inc, "replayed", "", [])
    proposal = {"action": "rollback_execute", "from_version": 2, "to_version": 1}
    store.transition(inc, "checkpointed", "gates passed", [], proposal=proposal, verdict="LOCALIZED_PROMPT")
    assert store.incident(inc)["proposal"] == proposal
    assert [i["id"] for i in store.open_incidents()] == [inc]
    store.transition(inc, "denied", "human said no", [])
    assert store.open_incidents() == []


def test_replay_round_trip(store):
    inc = store.open_incident("eval_score", [])
    rid = store.save_replay(inc, {"versions": [1, 2]}, [{"trace_id": "t1", "raw": "{}"}])
    assert store.replay(rid)["outputs"][0]["trace_id"] == "t1"


def test_last_unresolved_close_ignores_verified_incidents(store):
    fixed = store.open_incident("eval_score", [])
    for status in ["planned", "replayed", "checkpointed", "approved", "applied", "verified"]:
        store.transition(fixed, status, "", [])
    assert store.last_unresolved_close() is None
    unfixed = store.open_incident("eval_score", [])
    store.transition(unfixed, "not_localized", "gates failed", [])
    assert store.last_unresolved_close()["id"] == unfixed


def test_incident_periods_can_leave_one_incident_out(store):
    a = store.open_incident("eval_score", [], "2026-09-26T06:00:00+00:00", 5)
    b = store.open_incident("eval_score", [], "2026-09-26T07:00:00+00:00", 5)
    assert len(store.incident_periods()) == 2
    starts = [p[0].isoformat() for p in store.incident_periods(exclude=a)]
    assert starts == ["2026-09-26T06:45:00+00:00"]


def test_notification_round_trips_and_keeps_the_decision(store):
    inc = store.open_incident("eval_score", [])
    assert store.notification(inc) is None
    store.save_notification(inc, "C1", "100.1", summary="prompt v2 dropped escalation", linear_url="https://linear.app/x/1")
    assert store.claim_decision(inc, "@ana", "call_1") is None
    store.save_notification(inc, "C1", "100.1")  # re-saving without prose keeps prose and decision
    n = store.notification(inc)
    assert (n["channel"], n["ts"], n["summary"], n["linear_url"], n["decided_by"]) == (
        "C1", "100.1", "prompt v2 dropped escalation", "https://linear.app/x/1", "@ana")
    assert n["decided_at"]


def test_only_the_first_decision_is_claimed(store):
    inc = store.open_incident("eval_score", [])
    assert store.claim_decision(inc, "@ana", "call_1") is None
    assert store.claim_decision(inc, "console", "call_1") == "@ana"
    store.release_decision(inc)
    assert store.notification(inc)["decided_call"] is None
    assert store.claim_decision(inc, "console", "call_1") is None
    assert store.notification(inc)["ts"] is None  # a console-only claim has no Slack message


def test_a_lock_for_an_older_call_is_stale(store):
    inc = store.open_incident("eval_score", [])
    assert store.claim_decision(inc, "@ana", "call_a") is None
    assert store.claim_decision(inc, "@bo", "call_b") is None  # re-issued call after resume: a new decision
    n = store.notification(inc)
    assert (n["decided_by"], n["decided_call"]) == ("@bo", "call_b")
    assert store.claim_decision(inc, "console", "call_b") == "@bo"


def test_second_claim_for_the_same_call_names_the_first_holder(store):
    inc = store.open_incident("eval_score", [])
    assert store.claim_decision(inc, "@ana", "call_a") is None
    assert store.claim_decision(inc, "@bo", "call_a") == "@ana"
    assert store.notification(inc)["decided_by"] == "@ana"


def test_existing_database_gains_the_decided_call_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
    create table notifications (incident_id text primary key, channel text, ts text, summary text,
                                linear_url text, decided_by text, decided_at text);
    insert into notifications (incident_id, decided_by) values ('inc_old', '@ana');
    """)
    conn.close()
    store = Store(path)
    Store(path)  # idempotent
    assert store.notification("inc_old")["decided_call"] is None
    assert store.claim_decision("inc_old", "@bo", "call_1") is None
