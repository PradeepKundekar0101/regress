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
