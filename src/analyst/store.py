"""Durable state, so a killed run resumes instead of starting over.

SQLite, one file, written after every stage. The brief asks that the process can
be killed mid-run and restarted without losing finished work, and the honest way
to get that is to persist each stage's output as it completes rather than to
hold a run in memory and hope.

Concurrency: SQLite in WAL mode handles two runs writing at once, and a run's
rows are keyed by run id so two runs over the same corpus never share state. The
lease table stops two workers picking up the same run.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Iterator

from .models import (
    Claim,
    Conflict,
    Decision,
    Finding,
    Provenance,
    RegisterRow,
    RunState,
    SourceDocument,
    StageRecord,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    corpus_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    corpus_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    name TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (corpus_id, sha256)
);
CREATE TABLE IF NOT EXISTS registers (
    corpus_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corpus_id TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    subject TEXT,
    because TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS leases (
    run_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_corpus ON audit(corpus_id, id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LeaseHeld(RuntimeError):
    """Another worker is already running this run."""


class Store:
    """All persistence for the system."""

    def __init__(self, path: pathlib.Path | str = "data/analyst.db") -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ runs

    def save_run(self, state: RunState) -> None:
        """Persist a run. Called after every stage, so a crash costs one stage."""
        payload = json.dumps(_run_to_dict(state))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, corpus_id, revision, stage, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    revision=excluded.revision,
                    stage=excluded.stage,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (state.run_id, state.corpus_id, state.revision, state.stage, _now(), _now(), payload),
            )

    def load_run(self, run_id: str) -> RunState | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return _run_from_dict(json.loads(row[0])) if row else None

    def latest_run(self, corpus_id: str) -> RunState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM runs WHERE corpus_id=? ORDER BY revision DESC, updated_at DESC LIMIT 1",
                (corpus_id,),
            ).fetchone()
        return _run_from_dict(json.loads(row[0])) if row else None

    def list_runs(self, corpus_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT run_id, corpus_id, revision, stage, updated_at FROM runs"
        params: tuple = ()
        if corpus_id:
            query += " WHERE corpus_id=?"
            params = (corpus_id,)
        query += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {"run_id": r[0], "corpus_id": r[1], "revision": r[2], "stage": r[3], "updated_at": r[4]}
            for r in rows
        ]

    # --------------------------------------------------------------- leases

    @contextmanager
    def lease(self, run_id: str, holder: str, seconds: float = 900.0) -> Iterator[None]:
        """Stop two workers from advancing the same run at once.

        Two *different* runs proceed in parallel freely; this only serialises
        work on one run id, which is what keeps a resumed run from racing the
        process that never actually died.
        """
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM leases WHERE expires_at < ?", (now,))
            try:
                conn.execute(
                    "INSERT INTO leases (run_id, holder, expires_at) VALUES (?, ?, ?)",
                    (run_id, holder, now + seconds),
                )
            except sqlite3.IntegrityError as exc:
                current = conn.execute(
                    "SELECT holder FROM leases WHERE run_id=?", (run_id,)
                ).fetchone()
                raise LeaseHeld(
                    f"run {run_id} is already being processed by {current[0] if current else 'another worker'}. "
                    f"Wait for it to finish, or the lease expires in {seconds:.0f}s."
                ) from exc
        try:
            yield
        finally:
            with self._connect() as conn:
                conn.execute("DELETE FROM leases WHERE run_id=? AND holder=?", (run_id, holder))

    # ------------------------------------------------------------ documents

    def known_hashes(self, corpus_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sha256 FROM documents WHERE corpus_id=?", (corpus_id,)
            ).fetchall()
        return {r[0] for r in rows}

    def remember_documents(self, corpus_id: str, documents: list[SourceDocument]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO documents (corpus_id, sha256, name, payload) VALUES (?, ?, ?, ?)",
                [(corpus_id, d.sha256, d.name, json.dumps(asdict(d))) for d in documents],
            )

    def load_documents(self, corpus_id: str) -> list[SourceDocument]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM documents WHERE corpus_id=? ORDER BY name", (corpus_id,)
            ).fetchall()
        return [SourceDocument(**json.loads(r[0])) for r in rows]

    # ------------------------------------------------------------- register

    def save_register(self, corpus_id: str, revision: int, rows: list[RegisterRow]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO registers (corpus_id, revision, updated_at, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(corpus_id) DO UPDATE SET
                    revision=excluded.revision,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (corpus_id, revision, _now(), json.dumps([_row_to_dict(r) for r in rows])),
            )

    def load_register(self, corpus_id: str) -> tuple[int, list[RegisterRow]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT revision, payload FROM registers WHERE corpus_id=?", (corpus_id,)
            ).fetchone()
        if not row:
            return 0, []
        return row[0], [_row_from_dict(d) for d in json.loads(row[1])]

    # ---------------------------------------------------------------- audit

    def record(
        self,
        corpus_id: str,
        actor: str,
        action: str,
        *,
        subject: str | None = None,
        because: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Append to the audit trail.

        This is how the system answers "what changed, when, and because of which
        source" at any moment. Append only, never updated.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit (corpus_id, at, actor, action, subject, because, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (corpus_id, _now(), actor, action, subject, because, detail),
            )

    def history(self, corpus_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT at, actor, action, subject, because, detail FROM audit"
                " WHERE corpus_id=? ORDER BY id DESC LIMIT ?",
                (corpus_id, limit),
            ).fetchall()
        return [
            {"at": r[0], "actor": r[1], "action": r[2], "subject": r[3],
             "because": r[4], "detail": r[5]}
            for r in rows
        ]


# --------------------------------------------------------- (de)serialisation


def _prov_to_dict(p: Provenance) -> dict[str, Any]:
    return asdict(p)


def _claim_to_dict(c: Claim) -> dict[str, Any]:
    return {
        "id": c.id, "subject": c.subject, "value": None if c.value is None else str(c.value),
        "value_type": type(c.value).__name__, "unit": c.unit,
        "provenance": _prov_to_dict(c.provenance), "extractor": c.extractor,
        "effective_from": c.effective_from.isoformat() if c.effective_from else None,
        "confidence": c.confidence,
    }


def _claim_from_dict(d: dict[str, Any]) -> Claim:
    from datetime import date as _date
    from decimal import Decimal as _Decimal

    raw = d.get("value")
    value: Any = raw
    if raw is not None:
        if d.get("value_type") == "Decimal":
            value = _Decimal(raw)
        elif d.get("value_type") == "date":
            value = _date.fromisoformat(raw)
    return Claim(
        id=d["id"], subject=d["subject"], value=value, unit=d["unit"],
        provenance=Provenance(**d["provenance"]), extractor=d["extractor"],
        effective_from=_date.fromisoformat(d["effective_from"]) if d.get("effective_from") else None,
        confidence=d.get("confidence", 1.0),
    )


def _row_to_dict(r: RegisterRow) -> dict[str, Any]:
    return {
        "subject": r.subject, "label": r.label, "value": r.value,
        "citations": [_prov_to_dict(p) for p in r.citations],
        "conflict_id": r.conflict_id, "note": r.note,
    }


def _row_from_dict(d: dict[str, Any]) -> RegisterRow:
    return RegisterRow(
        subject=d["subject"], label=d["label"], value=d["value"],
        citations=[Provenance(**p) for p in d.get("citations", [])],
        conflict_id=d.get("conflict_id"), note=d.get("note"),
    )


def _run_to_dict(s: RunState) -> dict[str, Any]:
    return {
        "run_id": s.run_id, "corpus_id": s.corpus_id, "revision": s.revision, "stage": s.stage,
        "documents": [asdict(d) for d in s.documents],
        "claims": [_claim_to_dict(c) for c in s.claims],
        "conflicts": [
            {"id": c.id, "subject": c.subject, "kind": c.kind, "explanation": c.explanation,
             "decision": c.decision.value, "chosen_claim_id": c.chosen_claim_id,
             "decided_by": c.decided_by, "note": c.note,
             "claims": [_claim_to_dict(x) for x in c.claims]}
            for c in s.conflicts
        ],
        "findings": [
            {"id": f.id, "rule_id": f.rule_id, "severity": f.severity, "subject": f.subject,
             "message": f.message, "decision": f.decision.value, "note": f.note,
             "citations": [_prov_to_dict(p) for p in f.citations]}
            for f in s.findings
        ],
        "rows": [_row_to_dict(r) for r in s.rows],
        "stages": [asdict(x) for x in s.stages],
        "notes": s.notes,
    }


def _run_from_dict(d: dict[str, Any]) -> RunState:
    return RunState(
        run_id=d["run_id"], corpus_id=d["corpus_id"], revision=d.get("revision", 0),
        stage=d.get("stage", "created"),
        documents=[SourceDocument(**x) for x in d.get("documents", [])],
        claims=[_claim_from_dict(x) for x in d.get("claims", [])],
        conflicts=[
            Conflict(
                id=c["id"], subject=c["subject"], kind=c["kind"], explanation=c["explanation"],
                claims=[_claim_from_dict(x) for x in c.get("claims", [])],
                decision=Decision(c.get("decision", "pending")),
                chosen_claim_id=c.get("chosen_claim_id"), decided_by=c.get("decided_by"),
                note=c.get("note"),
            )
            for c in d.get("conflicts", [])
        ],
        findings=[
            Finding(
                id=f["id"], rule_id=f["rule_id"], severity=f["severity"], subject=f["subject"],
                message=f["message"], decision=Decision(f.get("decision", "pending")),
                note=f.get("note"),
                citations=[Provenance(**p) for p in f.get("citations", [])],
            )
            for f in d.get("findings", [])
        ],
        rows=[_row_from_dict(r) for r in d.get("rows", [])],
        stages=[StageRecord(**x) for x in d.get("stages", [])],
        notes=d.get("notes", []),
    )
