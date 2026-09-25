"""Durable agent state in SQLite: incidents, their state transitions, evidence and replays.

Every number Regress reports is an evidence row, and every state change is a transition row
that names the evidence it rests on. The state machine is enforced here, not in prompts.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / ".regress" / "state.sqlite"

# Allowed transitions. Only approved -> applied touches production.
TRANSITIONS: dict[str, set[str]] = {
    "detected": {"planned", "not_localized", "insufficient_data"},
    "planned": {"replayed", "not_localized", "insufficient_data"},
    "replayed": {"checkpointed", "not_localized"},
    "checkpointed": {"approved", "denied", "conflict"},
    "approved": {"applied", "conflict"},
    "applied": {"verified", "verify_failed"},
}
TERMINAL = {"not_localized", "insufficient_data", "denied", "conflict", "verified", "verify_failed"}

SCHEMA = """
create table if not exists incidents (
  id          text primary key,
  created_at  text not null,
  status      text not null,
  signal      text,          -- comma-separated alarming signals at detection
  detected_as_of text,       -- end of the window that alarmed; localisation re-runs against it
  window_minutes integer,
  verdict     text,
  proposal    text,          -- frozen JSON once checkpointed
  applied_at  text
);
create table if not exists transitions (
  id           integer primary key autoincrement,
  incident_id  text not null references incidents(id),
  ts           text not null,
  from_status  text,
  to_status    text not null,
  reason       text not null,
  evidence_ids text not null  -- JSON list
);
create table if not exists evidence (
  id           text primary key,
  incident_id  text references incidents(id),
  created_at   text not null,
  label        text not null,
  value        real,
  unit         text not null,
  window_from  text,
  window_to    text,
  source       text not null, -- JSON {kind, query?, params?, trace_ids?, url?}
  computed_by  text not null
);
create table if not exists replays (
  id           text primary key,
  incident_id  text not null references incidents(id),
  created_at   text not null,
  spec         text not null, -- JSON {versions, models, trace_ids}
  outputs      text not null, -- JSON list of {trace_id, golden_id, arm, raw, latency_ms, tokens_in, tokens_out, error}
  verification text           -- JSON result of verify_report, once the agent's report is checked
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TransitionError(RuntimeError):
    pass


@dataclass
class Evidence:
    label: str
    value: float | None
    unit: str
    source: dict
    computed_by: str
    window_from: str | None = None
    window_to: str | None = None
    id: str = field(default_factory=lambda: f"ev_{uuid.uuid4().hex[:8]}")

    def as_dict(self) -> dict:
        return asdict(self)


class Store:
    def __init__(self, path: Path = DEFAULT_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma synchronous=full")
        try:
            yield conn
        finally:
            conn.close()

    # --- evidence -----------------------------------------------------------------------

    def add_evidence(self, incident_id: str | None, items: list[Evidence]) -> list[dict]:
        with self._conn() as conn:
            for ev in items:
                conn.execute(
                    "insert into evidence values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ev.id, incident_id, now_iso(), ev.label, ev.value, ev.unit,
                     ev.window_from, ev.window_to, json.dumps(ev.source), ev.computed_by),
                )
        return [ev.as_dict() for ev in items]

    def attach_evidence(self, incident_id: str, evidence_ids: list[str]) -> None:
        with self._conn() as conn:
            conn.executemany(
                "update evidence set incident_id = ? where id = ? and incident_id is null",
                [(incident_id, eid) for eid in evidence_ids],
            )

    def evidence(self, incident_id: str) -> dict[str, dict]:
        with self._conn() as conn:
            rows = conn.execute("select * from evidence where incident_id = ?", (incident_id,)).fetchall()
        return {r["id"]: {**dict(r), "source": json.loads(r["source"])} for r in rows}

    # --- incidents and the state machine ------------------------------------------------

    def open_incident(self, signal: str, evidence_ids: list[str], detected_as_of: str | None = None,
                      window_minutes: int | None = None) -> str:
        incident_id = f"inc_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
        with self._conn() as conn:
            conn.execute("begin immediate")
            conn.execute(
                "insert into incidents (id, created_at, status, signal, detected_as_of, window_minutes) "
                "values (?, ?, 'detected', ?, ?, ?)",
                (incident_id, now_iso(), signal, detected_as_of, window_minutes),
            )
            conn.execute(
                "insert into transitions (incident_id, ts, from_status, to_status, reason, evidence_ids) "
                "values (?, ?, null, 'detected', ?, ?)",
                (incident_id, now_iso(), f"detector alarm on {signal}", json.dumps(evidence_ids)),
            )
            conn.execute("commit")
        self.attach_evidence(incident_id, evidence_ids)
        return incident_id

    def open_incidents(self) -> list[dict]:
        placeholders = ",".join("?" * len(TERMINAL))
        with self._conn() as conn:
            rows = conn.execute(
                f"select * from incidents where status not in ({placeholders}) order by created_at desc",
                tuple(TERMINAL),
            ).fetchall()
        return [self._incident_dict(r) for r in rows]

    def last_unresolved_close(self) -> dict | None:
        """The most recent incident that ended without a verified fix, with the time it ended."""
        with self._conn() as conn:
            row = conn.execute(
                """select i.id, i.status, max(t.ts) as closed_at from incidents i join transitions t on t.incident_id = i.id
                   where i.status in ('not_localized', 'insufficient_data', 'denied', 'conflict', 'verify_failed')
                   group by i.id order by closed_at desc limit 1""").fetchone()
        return dict(row) if row else None

    def incident_periods(self, lead_minutes: int = 10, tail_minutes: int = 3) -> list[tuple[datetime, datetime]]:
        """Periods of known incidents, to keep their traffic out of detector baselines.

        From `lead_minutes` before the alarming window (the regression began before it was detected) to
        `tail_minutes` after the incident's last transition (prompt caches drain after a rollback);
        open incidents run to now.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """select i.detected_as_of, i.window_minutes, i.status, max(t.ts) as last_ts
                   from incidents i join transitions t on t.incident_id = i.id
                   where i.detected_as_of is not null group by i.id""").fetchall()
        now = datetime.now(timezone.utc)
        periods = []
        for r in rows:
            start = datetime.fromisoformat(r["detected_as_of"]) - timedelta(minutes=(r["window_minutes"] or 5) + lead_minutes)
            end = now if r["status"] not in TERMINAL else datetime.fromisoformat(r["last_ts"]) + timedelta(minutes=tail_minutes)
            periods.append((start, end))
        return periods

    def incident(self, incident_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute("select * from incidents where id = ?", (incident_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown incident {incident_id}")
            transitions = conn.execute(
                "select ts, from_status, to_status, reason, evidence_ids from transitions "
                "where incident_id = ? order by id", (incident_id,),
            ).fetchall()
        result = self._incident_dict(row)
        result["transitions"] = [{**dict(t), "evidence_ids": json.loads(t["evidence_ids"])} for t in transitions]
        return result

    def transition(self, incident_id: str, to_status: str, reason: str, evidence_ids: list[str],
                   **fields) -> dict:
        """Move an incident to `to_status` if the state machine allows it, atomically."""
        allowed_fields = {"verdict", "proposal", "applied_at"}
        if set(fields) - allowed_fields:
            raise ValueError(f"cannot set {set(fields) - allowed_fields}")
        with self._conn() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select status from incidents where id = ?", (incident_id,)).fetchone()
            if row is None:
                conn.execute("rollback")
                raise KeyError(f"unknown incident {incident_id}")
            current = row["status"]
            if to_status not in TRANSITIONS.get(current, set()):
                conn.execute("rollback")
                raise TransitionError(f"{incident_id}: {current} -> {to_status} is not allowed")
            if "proposal" in fields and fields["proposal"] is not None:
                fields["proposal"] = json.dumps(fields["proposal"])
            sets = ", ".join(["status = ?"] + [f"{k} = ?" for k in fields])
            conn.execute(f"update incidents set {sets} where id = ?", (to_status, *fields.values(), incident_id))
            conn.execute(
                "insert into transitions (incident_id, ts, from_status, to_status, reason, evidence_ids) "
                "values (?, ?, ?, ?, ?, ?)",
                (incident_id, now_iso(), current, to_status, reason, json.dumps(evidence_ids)),
            )
            conn.execute("commit")
        return self.incident(incident_id)

    # --- replays ------------------------------------------------------------------------

    def save_replay(self, incident_id: str, spec: dict, outputs: list[dict]) -> str:
        replay_id = f"rp_{uuid.uuid4().hex[:8]}"
        with self._conn() as conn:
            conn.execute(
                "insert into replays (id, incident_id, created_at, spec, outputs) values (?, ?, ?, ?, ?)",
                (replay_id, incident_id, now_iso(), json.dumps(spec), json.dumps(outputs)),
            )
        return replay_id

    def replay(self, replay_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute("select * from replays where id = ?", (replay_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown replay {replay_id}")
        return {**dict(row), "spec": json.loads(row["spec"]), "outputs": json.loads(row["outputs"]),
                "verification": json.loads(row["verification"]) if row["verification"] else None}

    def set_replay_verification(self, replay_id: str, verification: dict) -> None:
        with self._conn() as conn:
            conn.execute("update replays set verification = ? where id = ?", (json.dumps(verification), replay_id))

    def latest_verified_replay(self, incident_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "select id from replays where incident_id = ? and json_extract(verification, '$.verified') = 1 "
                "order by created_at desc limit 1", (incident_id,)).fetchone()
        return self.replay(row["id"]) if row else None

    @staticmethod
    def _incident_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["proposal"] = json.loads(d["proposal"]) if d["proposal"] else None
        return d
